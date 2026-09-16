"""Privacy-minimal, preference-aware notification delivery."""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time
from enum import StrEnum
from typing import Protocol

SAFE = re.compile(r"^[A-Za-z0-9._:/-]{3,180}$")


class Channel(StrEnum):
    IN_APP = "IN_APP"
    PUSH = "PUSH"
    SMS = "SMS"
    EMAIL = "EMAIL"


class NotificationEvent(StrEnum):
    DELIVERY_COMPLETE = "DELIVERY_COMPLETE"
    OFFER = "OFFER"
    ASSIGNMENT = "ASSIGNMENT"
    PACKAGING = "PACKAGING"
    SETTLEMENT = "SETTLEMENT"
    DISPUTE = "DISPUTE"
    INCIDENT = "INCIDENT"
    INSURANCE_CASE = "INSURANCE_CASE"
    BRANCH_EMERGENCY = "BRANCH_EMERGENCY"


class DeliveryState(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RETRY = "RETRY"
    DEAD_LETTER = "DEAD_LETTER"


class NoticeErrorCode(StrEnum):
    FORBIDDEN = "FORBIDDEN"
    BRANCH_SCOPE = "BRANCH_SCOPE"
    RAW_PII = "RAW_PII"
    INVALID_REFERENCE = "INVALID_REFERENCE"
    PREFERENCE_BLOCKED = "PREFERENCE_BLOCKED"
    QUIET_HOURS = "QUIET_HOURS"
    MANDATORY_ABUSE = "MANDATORY_ABUSE"
    DUPLICATE = "DUPLICATE"
    CALLBACK_INVALID = "CALLBACK_INVALID"
    CALLBACK_REPLAY = "CALLBACK_REPLAY"
    DEEP_LINK_INVALID = "DEEP_LINK_INVALID"
    ARKAON_AUTHORITY_DENIED = "ARKAON_AUTHORITY_DENIED"


class NoticeRejected(RuntimeError):
    def __init__(self, code: NoticeErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True)
class NoticePrincipal:
    actor_id: str
    branch_id: str
    recipient_id: str | None = None
    staff: bool = False


@dataclass(frozen=True)
class Preference:
    recipient_id: str
    branch_id: str
    contact_vault_ref: str | None
    channels: frozenset[Channel]
    purposes: frozenset[str]
    quiet_start: time | None = None
    quiet_end: time | None = None
    locale: str = "ko-KR"


@dataclass(frozen=True)
class NoticeCommand:
    recipient_id: str
    branch_id: str
    event: NotificationEvent
    event_id: str
    purpose: str
    variables: Mapping[str, str]
    mandatory: bool = False


@dataclass(frozen=True)
class OutboxNotice:
    notice_id: str
    recipient_id: str
    branch_id: str
    event: NotificationEvent
    event_id: str
    sequence: int
    channel: Channel
    template_version: int
    lockscreen_text: str
    deep_link: str
    contact_vault_ref: str | None
    state: DeliveryState = DeliveryState.PENDING
    attempts: int = 0
    provider_ref: str | None = None
    acknowledgement_limits_rights: bool = False


class DeliveryProvider(Protocol):
    def send(self, notice: OutboxNotice) -> str: ...


class DeliveryProviderError(RuntimeError):
    """Retryable or permanent provider boundary failure."""


TEMPLATES: dict[tuple[NotificationEvent, str], tuple[int, str]] = {
    (event, "ko-KR"): (1, text)
    for event, text in {
        NotificationEvent.DELIVERY_COMPLETE: "배송이 완료되었습니다.",
        NotificationEvent.OFFER: "새 배차 제안이 있습니다.",
        NotificationEvent.ASSIGNMENT: "배차 상태가 변경되었습니다.",
        NotificationEvent.PACKAGING: "포장 상태가 업데이트되었습니다.",
        NotificationEvent.SETTLEMENT: "정산 내역이 업데이트되었습니다.",
        NotificationEvent.DISPUTE: "분쟁 처리 상태가 변경되었습니다.",
        NotificationEvent.INCIDENT: "안전사고 지원 상태가 변경되었습니다.",
        NotificationEvent.INSURANCE_CASE: "보험 지원 상태가 변경되었습니다.",
        NotificationEvent.BRANCH_EMERGENCY: "지사 긴급 안전 안내가 있습니다.",
    }.items()
}

MANDATORY_EVENTS = {NotificationEvent.INCIDENT, NotificationEvent.BRANCH_EMERGENCY}
FORBIDDEN_VARIABLES = {"address", "phone", "email", "precise_location", "rider_name", "order_contents", "medical"}


class NotificationService:
    def __init__(self, deep_link_secret: bytes) -> None:
        if len(deep_link_secret) < 16:
            raise ValueError("deep link secret too short")
        self.secret = deep_link_secret
        self.preferences: dict[tuple[str, str], Preference] = {}
        self.outbox: dict[str, OutboxNotice] = {}
        self.idempotency: dict[tuple[str, NotificationEvent, str, Channel], str] = {}
        self.sequences: dict[str, int] = {}
        self.callback_ids: set[str] = set()
        self.audit: list[dict[str, str]] = []

    def set_preference(self, principal: NoticePrincipal, preference: Preference) -> Preference:
        if principal.recipient_id != preference.recipient_id or principal.branch_id != preference.branch_id:
            raise NoticeRejected(NoticeErrorCode.FORBIDDEN)
        if preference.contact_vault_ref is not None:
            self._vault(preference.contact_vault_ref)
        if preference.locale != "ko-KR":
            raise NoticeRejected(NoticeErrorCode.INVALID_REFERENCE)
        saved = replace(preference, channels=preference.channels | {Channel.IN_APP})
        self.preferences[(saved.branch_id, saved.recipient_id)] = saved
        self._audit("PREFERENCE_UPDATED", saved.recipient_id, principal)
        return saved

    def enqueue(self, principal: NoticePrincipal, command: NoticeCommand, now: datetime) -> tuple[OutboxNotice, ...]:
        if not principal.staff or principal.branch_id != command.branch_id:
            raise NoticeRejected(NoticeErrorCode.BRANCH_SCOPE)
        for value in (command.recipient_id, command.branch_id, command.event_id, command.purpose):
            self._safe(value)
        if FORBIDDEN_VARIABLES & set(command.variables):
            raise NoticeRejected(NoticeErrorCode.RAW_PII)
        if command.mandatory != (command.event in MANDATORY_EVENTS):
            raise NoticeRejected(NoticeErrorCode.MANDATORY_ABUSE)
        preference = self.preferences.get((command.branch_id, command.recipient_id))
        if preference is None:
            preference = Preference(command.recipient_id, command.branch_id, None, frozenset({Channel.IN_APP}), frozenset())
        channels = preference.channels if command.mandatory else frozenset(
            channel for channel in preference.channels if command.purpose in preference.purposes
        )
        if not channels:
            raise NoticeRejected(NoticeErrorCode.PREFERENCE_BLOCKED)
        if not command.mandatory and self._quiet(preference, now):
            channels = frozenset({Channel.IN_APP}) if Channel.IN_APP in channels else frozenset()
            if not channels:
                raise NoticeRejected(NoticeErrorCode.QUIET_HOURS)
        created = []
        for channel in sorted(channels, key=lambda item: item.value):
            key = (command.recipient_id, command.event, command.event_id, channel)
            if key in self.idempotency:
                created.append(self.outbox[self.idempotency[key]])
                continue
            if channel is not Channel.IN_APP and preference.contact_vault_ref is None:
                continue
            sequence = self.sequences.get(command.recipient_id, 0) + 1
            self.sequences[command.recipient_id] = sequence
            notice_id = f"notice_{uuid.uuid4().hex}"
            version, text = TEMPLATES[(command.event, preference.locale)]
            deep_link = self._deep_link(notice_id, command.recipient_id, command.branch_id)
            notice = OutboxNotice(notice_id, command.recipient_id, command.branch_id, command.event,
                                  command.event_id, sequence, channel, version, text, deep_link,
                                  preference.contact_vault_ref if channel is not Channel.IN_APP else None)
            self.outbox[notice_id] = notice
            self.idempotency[key] = notice_id
            created.append(notice)
            self._audit("NOTICE_QUEUED", notice_id, principal)
        return tuple(created)

    def deliver(self, notice_id: str, providers: Mapping[Channel, DeliveryProvider]) -> OutboxNotice:
        notice = self.outbox[notice_id]
        earlier = [n for n in self.outbox.values() if n.recipient_id == notice.recipient_id and n.sequence < notice.sequence and n.state in {DeliveryState.PENDING, DeliveryState.RETRY}]
        if earlier:
            raise NoticeRejected(NoticeErrorCode.DUPLICATE)
        try:
            provider_ref = providers[notice.channel].send(notice)
            self._safe(provider_ref)
            updated = replace(notice, state=DeliveryState.SENT, attempts=notice.attempts + 1, provider_ref=provider_ref)
        except DeliveryProviderError:
            attempts = notice.attempts + 1
            state = DeliveryState.DEAD_LETTER if attempts >= 3 else DeliveryState.RETRY
            updated = replace(notice, state=state, attempts=attempts)
        self.outbox[notice_id] = updated
        return updated

    def callback(self, callback_id: str, notice_id: str, status: str, signature: str, verifier: CallbackVerifier) -> OutboxNotice:
        if callback_id in self.callback_ids:
            raise NoticeRejected(NoticeErrorCode.CALLBACK_REPLAY)
        payload = f"{callback_id}.{notice_id}.{status}".encode()
        if not verifier.verify(payload, signature):
            raise NoticeRejected(NoticeErrorCode.CALLBACK_INVALID)
        self.callback_ids.add(callback_id)
        notice = self.outbox[notice_id]
        state = DeliveryState.ACKNOWLEDGED if status == "ACKNOWLEDGED" else DeliveryState.SENT
        updated = replace(notice, state=state, acknowledgement_limits_rights=False)
        self.outbox[notice_id] = updated
        return updated

    def resolve_link(self, principal: NoticePrincipal, token: str) -> OutboxNotice:
        parts = token.split(".")
        if len(parts) != 2:
            raise NoticeRejected(NoticeErrorCode.DEEP_LINK_INVALID)
        notice = self.outbox.get(parts[0])
        if notice is None or principal.recipient_id != notice.recipient_id or principal.branch_id != notice.branch_id:
            raise NoticeRejected(NoticeErrorCode.DEEP_LINK_INVALID)
        expected = hmac.new(self.secret, f"{notice.notice_id}:{notice.recipient_id}:{notice.branch_id}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(parts[1], expected):
            raise NoticeRejected(NoticeErrorCode.DEEP_LINK_INVALID)
        return notice

    def arkaon_target(self, *, coercive: bool, ranking_or_penalty: bool) -> None:
        if coercive or ranking_or_penalty:
            raise NoticeRejected(NoticeErrorCode.ARKAON_AUTHORITY_DENIED)

    def _deep_link(self, notice_id: str, recipient: str, branch: str) -> str:
        digest = hmac.new(self.secret, f"{notice_id}:{recipient}:{branch}".encode(), hashlib.sha256).hexdigest()
        return f"{notice_id}.{digest}"

    @staticmethod
    def _quiet(preference: Preference, now: datetime) -> bool:
        if preference.quiet_start is None or preference.quiet_end is None:
            return False
        local = now.timetz().replace(tzinfo=None)
        if preference.quiet_start <= preference.quiet_end:
            return preference.quiet_start <= local < preference.quiet_end
        return local >= preference.quiet_start or local < preference.quiet_end

    @staticmethod
    def _safe(value: str) -> None:
        if not isinstance(value, str) or not SAFE.fullmatch(value):
            raise NoticeRejected(NoticeErrorCode.INVALID_REFERENCE)

    @classmethod
    def _vault(cls, value: str) -> None:
        if not isinstance(value, str) or not value.startswith("vault://"):
            raise NoticeRejected(NoticeErrorCode.RAW_PII)
        cls._safe(value)

    def _audit(self, action: str, subject: str, principal: NoticePrincipal) -> None:
        self.audit.append({"action": action, "subject": subject, "actor": principal.actor_id,
                           "branch": principal.branch_id, "at": datetime.now(UTC).isoformat()})


class CallbackVerifier:
    def __init__(self, secret: bytes) -> None:
        self.secret = secret

    def sign(self, payload: bytes) -> str:
        return hmac.new(self.secret, payload, hashlib.sha256).hexdigest()

    def verify(self, payload: bytes, signature: str) -> bool:
        return hmac.compare_digest(self.sign(payload), signature)
