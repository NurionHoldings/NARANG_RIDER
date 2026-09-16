"""Framework-neutral `/api/v1/rider-incidents` transport boundary."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from .incident_support import (
    IncidentKind,
    IncidentPrincipal,
    IncidentRejected,
    IncidentReportCommand,
    IncidentService,
)
from .resource_limits import JsonBudget, ResourceLimitExceeded, bounded_json_object

CASE_PATH = re.compile(r"^/api/v1/rider-incidents/([A-Za-z0-9_-]{3,80})$")
ACTION_PATH = re.compile(
    r"^/api/v1/rider-incidents/([A-Za-z0-9_-]{3,80})/(triage|human-assign|submit|resolve|appeal|correct|evidence-grants)$"
)


@dataclass(frozen=True)
class IncidentHttpRequest:
    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes = b""


@dataclass(frozen=True)
class IncidentHttpResponse:
    status: int
    body: Mapping[str, object]


class IncidentApi:
    """Requires an already verified server principal; request roles are ignored."""

    def __init__(self, service: IncidentService) -> None:
        self.service = service

    def handle(
        self, principal: IncidentPrincipal, request: IncidentHttpRequest
    ) -> IncidentHttpResponse:
        try:
            if request.method == "POST" and request.path == "/api/v1/rider-incidents":
                return self._report(principal, request)
            case_match = CASE_PATH.fullmatch(request.path)
            if request.method == "GET" and case_match:
                return IncidentHttpResponse(
                    200, self._case(self.service.get(principal, case_match.group(1)))
                )
            action_match = ACTION_PATH.fullmatch(request.path)
            if request.method == "POST" and action_match:
                return self._action(
                    principal, request, action_match.group(1), action_match.group(2)
                )
            return IncidentHttpResponse(404, {"error": "ROUTE_NOT_FOUND"})
        except IncidentRejected as error:
            status = (
                404
                if error.code.value == "NOT_FOUND"
                else 403
                if error.code.value in {"FORBIDDEN", "BRANCH_SCOPE"}
                else 409
            )
            return IncidentHttpResponse(status, {"error": error.code.value})
        except (ValueError, TypeError, json.JSONDecodeError):
            return IncidentHttpResponse(400, {"error": "INVALID_REQUEST"})

    def _report(
        self, principal: IncidentPrincipal, request: IncidentHttpRequest
    ) -> IncidentHttpResponse:
        body = self._body(request)
        allowed = {
            "assignment_id",
            "kind",
            "coarse_zone",
            "narrative_vault_ref",
            "medical_vault_ref",
        }
        if set(body) - allowed or not {
            "assignment_id",
            "kind",
            "coarse_zone",
            "narrative_vault_ref",
        } <= set(body):
            raise ValueError
        idempotency = request.headers.get("Idempotency-Key", "")
        command = IncidentReportCommand(
            assignment_id=self._str(body, "assignment_id"),
            kind=IncidentKind(self._str(body, "kind")),
            idempotency_key=idempotency,
            coarse_zone=self._str(body, "coarse_zone"),
            narrative_vault_ref=self._str(body, "narrative_vault_ref"),
            medical_vault_ref=self._optional_str(body.get("medical_vault_ref")),
        )
        case, stop, earnings = self.service.report(principal, command)
        return IncidentHttpResponse(
            201,
            {
                "case": self._case(case),
                "safety_stop": asdict(stop),
                "earnings_protection": asdict(earnings),
            },
        )

    def _action(
        self, principal: IncidentPrincipal, request: IncidentHttpRequest, case_id: str, action: str
    ) -> IncidentHttpResponse:
        body = self._body(request)
        if action == "triage":
            case = self.service.triage(principal, case_id)
        elif action == "human-assign":
            case = self.service.assign_human(principal, case_id, self._str(body, "assignee_id"))
        elif action == "submit":
            case = self.service.submit(principal, case_id, self._str(body, "channel"))
        elif action == "resolve":
            case = self.service.resolve(principal, case_id, self._str(body, "resolution_ref"))
        elif action == "appeal":
            case = self.service.appeal(principal, case_id, self._str(body, "reason_vault_ref"))
        elif action == "correct":
            case = self.service.correct(principal, case_id, self._str(body, "correction_ref"))
        else:
            grant = self.service.evidence_grant(principal, case_id, self._str(body, "purpose"))
            return IncidentHttpResponse(201, {"grant_ref": grant, "single_use": True})
        return IncidentHttpResponse(200, self._case(case))

    @staticmethod
    def _body(request: IncidentHttpRequest) -> dict[str, object]:
        if request.headers.get("Content-Type") != "application/json" or len(request.body) > 32_768:
            raise ValueError
        try:
            return bounded_json_object(request.body, JsonBudget(max_bytes=32_768))
        except ResourceLimitExceeded as error:
            raise ValueError from error

    @staticmethod
    def _str(body: Mapping[str, object], key: str) -> str:
        value = body.get(key)
        if not isinstance(value, str):
            raise TypeError
        return value

    @staticmethod
    def _optional_str(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError
        return value

    @staticmethod
    def _case(case: object) -> dict[str, object]:
        data = asdict(case)  # type: ignore[arg-type]
        data["kind"] = data["kind"].value
        data["state"] = data["state"].value
        return data
