"""HTTP DTO boundary for notification preferences, center and enqueue commands."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, time

from .notifications import (
    Channel,
    NoticeCommand,
    NoticePrincipal,
    NoticeRejected,
    NotificationEvent,
    NotificationService,
    Preference,
)


@dataclass(frozen=True)
class NotificationRequest:
    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes = b""


@dataclass(frozen=True)
class NotificationResponse:
    status: int
    body: object


class NotificationApi:
    def __init__(self, service: NotificationService) -> None:
        self.service = service

    def handle(self, principal: NoticePrincipal, request: NotificationRequest, now: datetime) -> NotificationResponse:
        try:
            if request.method == "PUT" and request.path == "/api/v1/notifications/preferences":
                body = self._body(request)
                preference = Preference(
                    principal.recipient_id or "", principal.branch_id,
                    self._optional(body.get("contact_vault_ref")),
                    frozenset(Channel(value) for value in self._list(body, "channels")),
                    frozenset(self._list(body, "purposes")),
                    self._clock(body.get("quiet_start")), self._clock(body.get("quiet_end")),
                )
                return NotificationResponse(200, asdict(self.service.set_preference(principal, preference)))
            if request.method == "GET" and request.path == "/api/v1/notifications":
                notices = [asdict(item) for item in self.service.outbox.values()
                           if item.recipient_id == principal.recipient_id and item.branch_id == principal.branch_id]
                return NotificationResponse(200, notices)
            if request.method == "POST" and request.path == "/api/v1/notifications":
                body = self._body(request)
                command = NoticeCommand(
                    self._str(body, "recipient_id"), principal.branch_id,
                    NotificationEvent(self._str(body, "event")),
                    request.headers.get("Idempotency-Key", ""), self._str(body, "purpose"),
                    self._mapping(body.get("variables", {})), bool(body.get("mandatory", False)),
                )
                return NotificationResponse(201, [asdict(item) for item in self.service.enqueue(principal, command, now)])
            return NotificationResponse(404, {"error": "ROUTE_NOT_FOUND"})
        except NoticeRejected as error:
            return NotificationResponse(409, {"error": error.code.value})
        except (ValueError, TypeError, json.JSONDecodeError):
            return NotificationResponse(400, {"error": "INVALID_REQUEST"})

    @staticmethod
    def _body(request: NotificationRequest) -> dict[str, object]:
        if request.headers.get("Content-Type") != "application/json" or len(request.body) > 32_768:
            raise ValueError
        body = json.loads(request.body)
        if not isinstance(body, dict):
            raise TypeError
        return body

    @staticmethod
    def _str(body: Mapping[str, object], key: str) -> str:
        value = body.get(key)
        if not isinstance(value, str):
            raise TypeError
        return value

    @staticmethod
    def _optional(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError
        return value

    @staticmethod
    def _list(body: Mapping[str, object], key: str) -> list[str]:
        value = body.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TypeError
        return value

    @staticmethod
    def _mapping(value: object) -> Mapping[str, str]:
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            raise TypeError
        return value

    @staticmethod
    def _clock(value: object) -> time | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError
        return time.fromisoformat(value)

