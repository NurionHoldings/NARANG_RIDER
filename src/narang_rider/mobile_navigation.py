"""Device-neutral, fail-closed navigation launch bridge.

The bridge does not discover apps for analytics, persist destinations, or launch without
a foreground rider gesture. Provider syntax remains certification data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import ClassVar
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from .map_integration import KnowledgeState, MapProviderKnowledgeRegistry


class MobilePlatform(StrEnum):
    ANDROID = "android"
    IOS = "ios"
    MOBILE_WEB = "mobile_web"


class LinkForm(StrEnum):
    ANDROID_APP_LINK = "android_app_link"
    ANDROID_INTENT = "android_intent"
    IOS_UNIVERSAL_LINK = "ios_universal_link"
    IOS_CUSTOM_SCHEME = "ios_custom_scheme"
    HTTPS_FALLBACK = "https_fallback"


class DeviceState(StrEnum):
    AVAILABLE = "available"
    APP_UNAVAILABLE = "app_unavailable"
    UPDATE_REQUIRED = "update_required"
    OS_RESTRICTED = "os_restricted"
    OFFLINE = "offline"


@dataclass(frozen=True)
class DeviceProfile:
    platform: MobilePlatform
    os_version: str
    state: DeviceState


class CapabilityProbe:
    """Injected local probe. Its result is never an analytics payload."""

    def __init__(self, probe: Callable[[str, LinkForm], DeviceState]) -> None:
        self._probe = probe

    def state(self, provider_id: str, form: LinkForm) -> DeviceState:
        return self._probe(provider_id, form)


@dataclass(frozen=True)
class CertifiedLinkForm:
    provider_id: str
    form: LinkForm
    scheme: str
    host: str
    path: str
    destination_parameter: str
    allowed_parameters: frozenset[str]
    official_evidence_digest: str
    certification_status: KnowledgeState

    def __post_init__(self) -> None:
        if self.certification_status is not KnowledgeState.VERIFIED:
            raise ValueError("provisional link forms remain disabled")
        if not self.official_evidence_digest:
            raise ValueError("official evidence digest is required")
        if self.scheme not in {"https", "geo"}:
            raise ValueError("scheme is not certified")
        if self.scheme == "https" and (
            not self.host
            or any(character in self.host for character in "@/:?#")
            or self.host.encode("idna").decode("ascii") != self.host
            or self.host != self.host.lower()
        ):
            raise ValueError("host must be lower-case ASCII/IDNA canonical")
        if self.path.startswith("//") or not self.path.startswith("/") or ".." in self.path:
            raise ValueError("path is not canonical")
        if self.destination_parameter not in self.allowed_parameters:
            raise ValueError("destination parameter is not allowlisted")


@dataclass(frozen=True)
class NavigationLaunchRequest:
    provider_id: str
    branch_id: str
    rider_id: str
    order_id: str
    assignment_id: str
    session_id: str
    destination_token: str
    launch_token: str
    idempotency_key: str
    foreground_user_gesture: bool
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class LaunchInstruction:
    launch_id: str
    target_url: str
    form: LinkForm
    expires_at: datetime
    browser_features: str
    message: str
    fallback_required: bool


class NavigationBridgeRejected(ValueError):
    pass


class SingleUseLaunchTokens:
    def __init__(self) -> None:
        self._consumed: set[str] = set()

    def consume(self, token: str, scope: str) -> None:
        del scope
        digest = sha256(token.encode()).hexdigest()
        if digest in self._consumed:
            raise NavigationBridgeRejected("launch token replay")
        self._consumed.add(digest)


@dataclass
class LocalNavigationPreference:
    """Device-local provider choice; never contains destination or route data."""

    provider_id: str | None = None
    ask_each_trip: bool = True

    def select_for_trip(self, provider_id: str) -> str:
        if not provider_id:
            raise NavigationBridgeRejected("provider choice is required")
        return provider_id


@dataclass
class ReturnStateStore:
    _states: dict[str, tuple[str, datetime]] = field(default_factory=dict)

    def issue(self, scope: str, expires_at: datetime) -> str:
        state = sha256(f"{scope}|{expires_at.isoformat()}".encode()).hexdigest()
        self._states[state] = (scope, expires_at)
        return state

    def consume(self, state: str, scope: str, now: datetime) -> None:
        stored = self._states.pop(state, None)
        if stored is None or stored[0] != scope or now >= stored[1]:
            raise NavigationBridgeRejected("invalid, replayed, or expired return state")


class MobileNavigationBridge:
    SAFE_MESSAGES: ClassVar[dict[DeviceState, str]] = {
        DeviceState.APP_UNAVAILABLE: "선택한 지도 앱을 사용할 수 없습니다. 다른 앱이나 시스템 지도를 선택해 주세요.",
        DeviceState.UPDATE_REQUIRED: "지도 앱 업데이트가 필요합니다. 시스템 지도 또는 목적지 복사를 이용할 수 있습니다.",
        DeviceState.OS_RESTRICTED: "기기 설정에서 외부 앱 열기가 제한되었습니다. 시스템 지도나 복사를 선택해 주세요.",
        DeviceState.OFFLINE: "인터넷에 연결되지 않았습니다. 자동 재시도하지 않습니다. 연결 후 직접 다시 시도해 주세요.",
    }

    def __init__(
        self,
        registry: MapProviderKnowledgeRegistry,
        forms: dict[tuple[str, MobilePlatform], tuple[CertifiedLinkForm, ...]],
        probe: CapabilityProbe,
        destination_resolver: Callable[[str, str, str, str], str],
        tokens: SingleUseLaunchTokens,
        returns: ReturnStateStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.registry = registry
        self.forms = forms
        self.probe = probe
        self.destination_resolver = destination_resolver
        self.tokens = tokens
        self.returns = returns
        self.clock = clock
        self._idempotency: dict[str, LaunchInstruction] = {}

    def launch(
        self, request: NavigationLaunchRequest, device: DeviceProfile
    ) -> LaunchInstruction:
        now = self.clock()
        scope = self._scope(request)
        if not request.foreground_user_gesture:
            raise NavigationBridgeRejected("background or silent launch is forbidden")
        if now >= request.expires_at or request.expires_at - request.issued_at > timedelta(minutes=2):
            raise NavigationBridgeRejected("launch request expired or exceeds two-minute TTL")
        if any(not value or self._has_control(value) for value in self._scope_values(request)):
            raise NavigationBridgeRejected("invalid rider, order, branch, or session scope")

        record = self.registry.records.get(request.provider_id)
        if (
            record is None
            or record.state is not KnowledgeState.VERIFIED
            or now >= record.expires_at
        ):
            raise NavigationBridgeRejected("provider official knowledge is not verified")

        idem = sha256(f"{scope}|{request.idempotency_key}".encode()).hexdigest()
        if idem in self._idempotency:
            return self._idempotency[idem]

        form = self._select_form(request.provider_id, device)
        state = self.probe.state(request.provider_id, form.form)
        if state is not DeviceState.AVAILABLE:
            return LaunchInstruction(
                launch_id=idem,
                target_url="",
                form=LinkForm.HTTPS_FALLBACK,
                expires_at=request.expires_at,
                browser_features="noopener,noreferrer",
                message=self.SAFE_MESSAGES[state],
                fallback_required=True,
            )

        self.tokens.consume(request.launch_token, scope)
        destination = self.destination_resolver(
            request.destination_token,
            request.order_id,
            request.rider_id,
            request.branch_id,
        )
        if not destination or self._has_control(destination):
            raise NavigationBridgeRejected("ephemeral destination resolution failed")
        return_state = self.returns.issue(scope, request.expires_at)
        target = self._build_url(form, destination, return_state)
        result = LaunchInstruction(
            launch_id=idem,
            target_url=target,
            form=form.form,
            expires_at=request.expires_at,
            browser_features="noopener,noreferrer",
            message="외부 지도 앱을 엽니다. 돌아오면 기존 배송업무를 계속하세요.",
            fallback_required=False,
        )
        self._idempotency[idem] = result
        return result

    def validate_return(self, state: str, request: NavigationLaunchRequest) -> None:
        self.returns.consume(state, self._scope(request), self.clock())

    def _select_form(
        self, provider_id: str, device: DeviceProfile
    ) -> CertifiedLinkForm:
        candidates = self.forms.get((provider_id, device.platform), ())
        verified = tuple(
            form for form in candidates if form.certification_status is KnowledgeState.VERIFIED
        )
        if not verified:
            raise NavigationBridgeRejected("no certified link form for this device")
        return verified[0]

    @staticmethod
    def _build_url(
        form: CertifiedLinkForm, destination: str, return_state: str
    ) -> str:
        parameters = {
            form.destination_parameter: destination,
            "return_state": return_state,
        }
        if "return_state" not in form.allowed_parameters:
            raise NavigationBridgeRejected("return state parameter is not certified")
        query = "&".join(
            f"{quote(key, safe='')}={quote(value, safe='')}"
            for key, value in parameters.items()
        )
        target = urlunsplit((form.scheme, form.host, form.path, query, ""))
        parsed = urlsplit(target)
        if (
            parsed.scheme != form.scheme
            or parsed.netloc != form.host
            or parsed.path != form.path
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise NavigationBridgeRejected("generated target left certified boundary")
        if any(key not in form.allowed_parameters for key, _ in parse_qsl(parsed.query)):
            raise NavigationBridgeRejected("generated query left certified boundary")
        return target

    @staticmethod
    def _scope(request: NavigationLaunchRequest) -> str:
        return (
            f"{request.branch_id}|{request.rider_id}|{request.order_id}|"
            f"{request.assignment_id}|{request.session_id}"
        )

    @staticmethod
    def _scope_values(request: NavigationLaunchRequest) -> tuple[str, ...]:
        return (
            request.branch_id,
            request.rider_id,
            request.order_id,
            request.assignment_id,
            request.session_id,
            request.idempotency_key,
            request.launch_token,
        )

    @staticmethod
    def _has_control(value: str) -> bool:
        return any(ord(character) < 32 or ord(character) == 127 for character in value)


@dataclass(frozen=True)
class DeviceCertificationArtifact:
    provider_id: str
    platform: MobilePlatform
    physical_device_model: str | None
    os_version: str | None
    official_scheme_verified: bool
    install_unavailable_update_offline_tested: bool
    return_state_tested: bool
    accessibility_tested: bool

    @property
    def status(self) -> KnowledgeState:
        complete = (
            self.physical_device_model
            and self.os_version
            and self.official_scheme_verified
            and self.install_unavailable_update_offline_tested
            and self.return_state_tested
            and self.accessibility_tested
        )
        return KnowledgeState.VERIFIED if complete else KnowledgeState.BLOCKED


class ArkaonDeviceAdapterAssistant:
    """May propose adapters/tests; cannot verify, install, or launch."""

    def propose(self, provider_id: str, platform: MobilePlatform) -> dict[str, object]:
        return {
            "provider_id": provider_id,
            "platform": platform.value,
            "proposal": "CertifiedLinkForm + synthetic compatibility matrix",
            "can_mark_verified": False,
            "can_install_or_launch": False,
            "requires": (
                "official source evidence",
                "Ethernian review",
                "physical-device certification",
                "operator promotion",
            ),
        }
