from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.map_integration import (
    KnowledgeState,
    MapCapability,
    MapProviderKnowledgeRegistry,
    OfficialSource,
    ProviderKnowledge,
)
from narang_rider.mobile_navigation import (
    ArkaonDeviceAdapterAssistant,
    CapabilityProbe,
    CertifiedLinkForm,
    DeviceCertificationArtifact,
    DeviceProfile,
    DeviceState,
    LinkForm,
    LocalNavigationPreference,
    MobileNavigationBridge,
    MobilePlatform,
    NavigationBridgeRejected,
    NavigationLaunchRequest,
    ReturnStateStore,
    SingleUseLaunchTokens,
)

NOW = datetime(2026, 9, 16, 12, tzinfo=UTC)


def registry(state: KnowledgeState = KnowledgeState.VERIFIED) -> MapProviderKnowledgeRegistry:
    result = MapProviderKnowledgeRegistry()
    result.register(
        ProviderKnowledge(
            provider_id="verified-map",
            version="1",
            jurisdiction="KR",
            capabilities=frozenset({MapCapability.NAVIGATION_HANDOFF}),
            state=state,
            official_sources=(
                OfficialSource.create(
                    "Official", "https://docs.example.com/map", "2026-09-16"
                ),
            ),
            last_verified=NOW,
            expires_at=NOW + timedelta(days=30),
            confidence=1,
            attribution="required",
            key_restrictions="no client secrets",
            terms_review="reviewed fixture",
        )
    )
    return result


def certified_form(platform: MobilePlatform) -> CertifiedLinkForm:
    form = {
        MobilePlatform.ANDROID: LinkForm.ANDROID_APP_LINK,
        MobilePlatform.IOS: LinkForm.IOS_UNIVERSAL_LINK,
        MobilePlatform.MOBILE_WEB: LinkForm.HTTPS_FALLBACK,
    }[platform]
    return CertifiedLinkForm(
        provider_id="verified-map",
        form=form,
        scheme="https",
        host="maps.example.com",
        path="/navigate",
        destination_parameter="destination",
        allowed_parameters=frozenset({"destination", "return_state"}),
        official_evidence_digest="sha256:official-fixture",
        certification_status=KnowledgeState.VERIFIED,
    )


def request(**changes: object) -> NavigationLaunchRequest:
    values = {
        "provider_id": "verified-map",
        "branch_id": "branch-1",
        "rider_id": "rider-1",
        "order_id": "order-1",
        "assignment_id": "assignment-1",
        "session_id": "session-1",
        "destination_token": "destination-token",
        "launch_token": "launch-token",
        "idempotency_key": "tap-1",
        "foreground_user_gesture": True,
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=1),
    }
    values.update(changes)
    return NavigationLaunchRequest(**values)


def bridge(
    platform: MobilePlatform = MobilePlatform.ANDROID,
    state: DeviceState = DeviceState.AVAILABLE,
    knowledge: KnowledgeState = KnowledgeState.VERIFIED,
) -> MobileNavigationBridge:
    return MobileNavigationBridge(
        registry(knowledge),
        {("verified-map", platform): (certified_form(platform),)},
        CapabilityProbe(lambda _provider, _form: state),
        lambda token, order, rider, branch: (
            "37.1,127.1"
            if (token, order, rider, branch)
            == ("destination-token", "order-1", "rider-1", "branch-1")
            else ""
        ),
        SingleUseLaunchTokens(),
        ReturnStateStore(),
        clock=lambda: NOW,
    )


@pytest.mark.parametrize(
    "platform",
    [MobilePlatform.ANDROID, MobilePlatform.IOS, MobilePlatform.MOBILE_WEB],
)
def test_synthetic_platform_matrix_launches_only_certified_https(
    platform: MobilePlatform,
) -> None:
    instruction = bridge(platform).launch(
        request(), DeviceProfile(platform, "synthetic", DeviceState.AVAILABLE)
    )
    assert instruction.target_url.startswith(
        "https://maps.example.com/navigate?destination=37.1%2C127.1&return_state="
    )
    assert instruction.browser_features == "noopener,noreferrer"
    assert instruction.fallback_required is False


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (DeviceState.APP_UNAVAILABLE, "사용할 수 없습니다"),
        (DeviceState.UPDATE_REQUIRED, "업데이트"),
        (DeviceState.OS_RESTRICTED, "제한"),
        (DeviceState.OFFLINE, "자동 재시도하지 않습니다"),
    ],
)
def test_unavailable_update_restriction_and_offline_are_accessible_fallbacks(
    state: DeviceState, message: str
) -> None:
    instruction = bridge(state=state).launch(
        request(), DeviceProfile(MobilePlatform.ANDROID, "synthetic", state)
    )
    assert instruction.target_url == ""
    assert instruction.fallback_required is True
    assert message in instruction.message


def test_provisional_provider_and_link_form_are_disabled() -> None:
    with pytest.raises(NavigationBridgeRejected):
        bridge(knowledge=KnowledgeState.PROVISIONAL).launch(
            request(),
            DeviceProfile(
                MobilePlatform.ANDROID, "synthetic", DeviceState.AVAILABLE
            ),
        )
    with pytest.raises(ValueError):
        CertifiedLinkForm(
            provider_id="p",
            form=LinkForm.IOS_CUSTOM_SCHEME,
            scheme="geo",
            host="",
            path="/open",
            destination_parameter="d",
            allowed_parameters=frozenset({"d"}),
            official_evidence_digest="proof",
            certification_status=KnowledgeState.PROVISIONAL,
        )


def test_background_launch_timeout_and_control_character_scope_are_rejected() -> None:
    device = DeviceProfile(
        MobilePlatform.ANDROID, "synthetic", DeviceState.AVAILABLE
    )
    for candidate in (
        request(foreground_user_gesture=False),
        request(expires_at=NOW),
        request(expires_at=NOW + timedelta(minutes=3)),
        request(order_id="order-1\nhttps://evil.example"),
    ):
        with pytest.raises(NavigationBridgeRejected):
            bridge().launch(candidate, device)


def test_duplicate_click_is_idempotent_but_launch_token_cannot_cross_order() -> None:
    service = bridge()
    device = DeviceProfile(
        MobilePlatform.ANDROID, "synthetic", DeviceState.AVAILABLE
    )
    assert service.launch(request(), device) == service.launch(request(), device)
    with pytest.raises(NavigationBridgeRejected):
        service.launch(
            request(
                order_id="order-2",
                idempotency_key="tap-2",
            ),
            device,
        )


def test_return_state_is_scope_bound_single_use_and_expires() -> None:
    service = bridge()
    device = DeviceProfile(
        MobilePlatform.ANDROID, "synthetic", DeviceState.AVAILABLE
    )
    instruction = service.launch(request(), device)
    state = dict(
        pair.split("=", 1)
        for pair in instruction.target_url.split("?", 1)[1].split("&")
    )["return_state"]
    service.validate_return(state, request())
    with pytest.raises(NavigationBridgeRejected):
        service.validate_return(state, request())


@pytest.mark.parametrize(
    "host",
    ["Maps.Example.com", "máp.example.com", "user@maps.example.com"],
)
def test_unicode_noncanonical_and_credential_hosts_are_rejected(host: str) -> None:
    with pytest.raises((ValueError, UnicodeError)):
        CertifiedLinkForm(
            provider_id="unsafe",
            form=LinkForm.HTTPS_FALLBACK,
            scheme="https",
            host=host,
            path="/navigate",
            destination_parameter="d",
            allowed_parameters=frozenset({"d"}),
            official_evidence_digest="proof",
            certification_status=KnowledgeState.VERIFIED,
        )


def test_local_preference_contains_no_route_and_still_supports_each_trip_choice() -> None:
    preference = LocalNavigationPreference()
    assert preference.ask_each_trip is True
    assert preference.select_for_trip("verified-map") == "verified-map"
    assert not hasattr(preference, "destination")
    assert not hasattr(preference, "coordinates")


def test_physical_device_artifact_stays_blocked_until_all_evidence_exists() -> None:
    artifact = DeviceCertificationArtifact(
        provider_id="verified-map",
        platform=MobilePlatform.IOS,
        physical_device_model=None,
        os_version=None,
        official_scheme_verified=False,
        install_unavailable_update_offline_tested=False,
        return_state_tested=False,
        accessibility_tested=False,
    )
    assert artifact.status is KnowledgeState.BLOCKED


def test_arkaon_can_only_propose_and_never_verify_install_or_launch() -> None:
    proposal = ArkaonDeviceAdapterAssistant().propose(
        "verified-map", MobilePlatform.ANDROID
    )
    assert proposal["can_mark_verified"] is False
    assert proposal["can_install_or_launch"] is False
