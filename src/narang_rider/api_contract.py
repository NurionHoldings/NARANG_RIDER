"""Versioned, framework-neutral HTTP contract for NARANG RIDER operations."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .application import (
    CommandCapability,
    CommandContext,
    IntakeCommandRejected,
    IntakeErrorCode,
    OrderIntakeApplicationService,
    OrderIntakeCommand,
    RiderCallRoute,
)
from .persistence import (
    ConcurrencyConflict,
    IdempotencyConflict,
    RecordKind,
    Repository,
    SensitiveDataRejected,
    UnitOfWorkFactory,
    canonical_payload_digest,
)

MAX_REQUEST_BYTES = 65_536
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ORDER_PATH = re.compile(r"^/api/v1/orders/([A-Za-z0-9_-]{1,64})/status$")
_RIDER_CALL_PATH = re.compile(r"^/api/v1/orders/([A-Za-z0-9_-]{1,64})/rider-calls$")


class ApiErrorCode(StrEnum):
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    BRANCH_SCOPE_MISMATCH = "BRANCH_SCOPE_MISMATCH"
    CONFLICT = "CONFLICT"
    IDEMPOTENCY_REQUIRED = "IDEMPOTENCY_REQUIRED"
    INVALID_CONTENT_TYPE = "INVALID_CONTENT_TYPE"
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    PII_POLICY_VIOLATION = "PII_POLICY_VIOLATION"
    RATE_LIMITED = "RATE_LIMITED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    ROUTE_NOT_FOUND = "ROUTE_NOT_FOUND"


class AuthVerificationError(RuntimeError):
    """Authentication boundary failure safe for stable mapping."""


@dataclass(frozen=True)
class VerifiedPrincipal:
    actor_id: str
    branch_id: str
    capabilities: frozenset[CommandCapability]


class AuthContextVerifier(Protocol):
    """Verifies transport credentials; implementations own cryptography."""

    def verify(self, authorization: str, requested_branch_id: str) -> VerifiedPrincipal: ...


class RateLimitHook(Protocol):
    def allow(self, principal: VerifiedPrincipal, route_id: str) -> bool: ...


@dataclass(frozen=True)
class HttpRequest:
    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes = b""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: Mapping[str, object]


@dataclass(frozen=True)
class RouteContract:
    route_id: str
    method: str
    path_template: str
    request_dto: str | None
    response_dto: str
    required_capability: CommandCapability
    idempotency_required: bool


ROUTE_MANIFEST = (
    RouteContract(
        "order_intake",
        "POST",
        "/api/v1/orders",
        "OrderIntakeV1",
        "OrderAcceptedV1",
        CommandCapability.INGEST_ORDER,
        True,
    ),
    RouteContract(
        "rider_call",
        "POST",
        "/api/v1/orders/{order_id}/rider-calls",
        "RiderCallV1",
        "RiderCallAcceptedV1",
        CommandCapability.CALL_RIDER,
        True,
    ),
    RouteContract(
        "order_status",
        "GET",
        "/api/v1/orders/{order_id}/status",
        None,
        "OrderStatusV1",
        CommandCapability.INGEST_ORDER,
        False,
    ),
)


class FakeAuthContextVerifier:
    """Test adapter. Tokens map to server-side principals, never request roles."""

    def __init__(self, principals: Mapping[str, VerifiedPrincipal]) -> None:
        self._principals = dict(principals)

    def verify(self, authorization: str, requested_branch_id: str) -> VerifiedPrincipal:
        principal = self._principals.get(authorization)
        if principal is None:
            raise AuthVerificationError("invalid credential")
        if principal.branch_id != requested_branch_id:
            raise AuthVerificationError("branch scope mismatch")
        return principal


class AllowAllRateLimit:
    def allow(self, principal: VerifiedPrincipal, route_id: str) -> bool:
        return True


class ApiContractHandler:
    """Routes validated requests into application and persistence boundaries."""

    def __init__(
        self,
        *,
        auth_verifier: AuthContextVerifier,
        rate_limit: RateLimitHook,
        intake: OrderIntakeApplicationService,
        repository: Repository,
        unit_of_work: UnitOfWorkFactory,
    ) -> None:
        self._auth_verifier = auth_verifier
        self._rate_limit = rate_limit
        self._intake = intake
        self._repository = repository
        self._unit_of_work = unit_of_work

    def handle(self, request: HttpRequest) -> HttpResponse:
        correlation_id = self._correlation_id(request.headers)
        route_id, order_id = self._match(request.method.upper(), request.path)
        if route_id is None:
            return self._error(404, ApiErrorCode.ROUTE_NOT_FOUND, correlation_id)
        branch_id = request.headers.get("X-Branch-Id", "")
        if not _SAFE_ID.fullmatch(branch_id):
            return self._error(400, ApiErrorCode.INVALID_REQUEST, correlation_id)
        authorization = request.headers.get("Authorization", "")
        if not authorization:
            return self._error(401, ApiErrorCode.AUTHENTICATION_REQUIRED, correlation_id)
        try:
            principal = self._auth_verifier.verify(authorization, branch_id)
        except AuthVerificationError:
            return self._error(401, ApiErrorCode.AUTHENTICATION_REQUIRED, correlation_id)
        contract = next(route for route in ROUTE_MANIFEST if route.route_id == route_id)
        if contract.required_capability not in principal.capabilities:
            return self._error(403, ApiErrorCode.AUTHORIZATION_DENIED, correlation_id)
        if not self._rate_limit.allow(principal, route_id):
            return self._error(429, ApiErrorCode.RATE_LIMITED, correlation_id)
        if contract.idempotency_required:
            idempotency_key = request.headers.get("Idempotency-Key", "")
            if not _SAFE_ID.fullmatch(idempotency_key):
                return self._error(400, ApiErrorCode.IDEMPOTENCY_REQUIRED, correlation_id)
        else:
            idempotency_key = ""

        if route_id == "order_status":
            return self._status(principal, order_id or "", correlation_id)
        validation = self._json_body(request, correlation_id)
        if isinstance(validation, HttpResponse):
            return validation
        if route_id == "order_intake":
            return self._order_intake(principal, validation, idempotency_key, correlation_id)
        return self._rider_call(
            principal, order_id or "", validation, idempotency_key, correlation_id
        )

    def _order_intake(
        self,
        principal: VerifiedPrincipal,
        body: dict[str, object],
        idempotency_key: str,
        correlation_id: str,
    ) -> HttpResponse:
        allowed = {
            "source_system",
            "source_order_id",
            "merchant_id",
            "total_won",
            "delivery_address_vault_ref",
            "recipient_phone_vault_ref",
            "rider_call_route",
            "attributes",
        }
        required = allowed - {"attributes"}
        if set(body) - allowed or not required <= set(body):
            return self._error(400, ApiErrorCode.INVALID_REQUEST, correlation_id)
        try:
            command = OrderIntakeCommand(
                source_system=self._string(body, "source_system"),
                source_order_id=self._string(body, "source_order_id"),
                idempotency_key=idempotency_key,
                branch_id=principal.branch_id,
                merchant_id=self._string(body, "merchant_id"),
                total_won=self._integer(body, "total_won"),
                delivery_address_vault_ref=self._string(body, "delivery_address_vault_ref"),
                recipient_phone_vault_ref=self._string(body, "recipient_phone_vault_ref"),
                rider_call_route=RiderCallRoute(self._string(body, "rider_call_route")),
                attributes=self._mapping(body.get("attributes", {})),
            )
            receipt = self._intake.ingest(
                CommandContext(principal.actor_id, principal.branch_id, principal.capabilities),
                command,
            )
        except (ValueError, TypeError):
            return self._error(400, ApiErrorCode.INVALID_REQUEST, correlation_id)
        except IntakeCommandRejected as error:
            return self._intake_error(error, correlation_id)
        return self._success(
            201,
            {
                "order_id": receipt.order_id,
                "status": "RECEIVED",
                "replayed": receipt.persistence_receipt.replayed,
            },
            correlation_id,
        )

    def _rider_call(
        self,
        principal: VerifiedPrincipal,
        order_id: str,
        body: dict[str, object],
        idempotency_key: str,
        correlation_id: str,
    ) -> HttpResponse:
        if set(body) != {"route"}:
            return self._error(400, ApiErrorCode.INVALID_REQUEST, correlation_id)
        order = self._repository.get(RecordKind.ORDER, principal.branch_id, order_id)
        if order is None:
            return self._error(404, ApiErrorCode.NOT_FOUND, correlation_id)
        try:
            route = RiderCallRoute(self._string(body, "route"))
            payload = {
                "branch_id": principal.branch_id,
                "order_id": order_id,
                "route": route.value,
                "requested_by": principal.actor_id,
            }
            unit = self._unit_of_work.begin(
                branch_id=principal.branch_id,
                idempotency_key=f"rider-call:{idempotency_key}",
                payload_digest=canonical_payload_digest(payload),
            )
            unit.put(RecordKind.RIDER_CALL, order_id, payload, expected_version=0)
            unit.put(
                RecordKind.OUTBOX_MESSAGE,
                f"rider-call:{order_id}:1",
                {
                    "branch_id": principal.branch_id,
                    "order_id": order_id,
                    "status": "RIDER_CALL_REQUESTED",
                    "sequence": 1,
                    "requires_ledger": False,
                },
                expected_version=0,
            )
            receipt = unit.commit()
        except (ValueError, TypeError):
            return self._error(400, ApiErrorCode.INVALID_REQUEST, correlation_id)
        except IdempotencyConflict:
            return self._error(409, ApiErrorCode.CONFLICT, correlation_id)
        except ConcurrencyConflict:
            return self._error(409, ApiErrorCode.CONFLICT, correlation_id)
        except SensitiveDataRejected:
            return self._error(400, ApiErrorCode.PII_POLICY_VIOLATION, correlation_id)
        return self._success(
            201,
            {
                "order_id": order_id,
                "route": route.value,
                "replayed": receipt.replayed,
            },
            correlation_id,
        )

    def _status(
        self, principal: VerifiedPrincipal, order_id: str, correlation_id: str
    ) -> HttpResponse:
        order = self._repository.get(RecordKind.ORDER, principal.branch_id, order_id)
        if order is None:
            return self._error(404, ApiErrorCode.NOT_FOUND, correlation_id)
        return self._success(
            200,
            {
                "order_id": order_id,
                "status": order.payload.get("state"),
                "version": order.version,
            },
            correlation_id,
        )

    @staticmethod
    def _match(method: str, path: str) -> tuple[str | None, str | None]:
        if method == "POST" and path == "/api/v1/orders":
            return "order_intake", None
        rider_call = _RIDER_CALL_PATH.fullmatch(path)
        if method == "POST" and rider_call:
            return "rider_call", rider_call.group(1)
        status = _ORDER_PATH.fullmatch(path)
        if method == "GET" and status:
            return "order_status", status.group(1)
        return None, None

    @staticmethod
    def _json_body(request: HttpRequest, correlation_id: str) -> dict[str, object] | HttpResponse:
        if len(request.body) > MAX_REQUEST_BYTES:
            return ApiContractHandler._error(413, ApiErrorCode.PAYLOAD_TOO_LARGE, correlation_id)
        content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            return ApiContractHandler._error(415, ApiErrorCode.INVALID_CONTENT_TYPE, correlation_id)
        try:
            parsed = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ApiContractHandler._error(400, ApiErrorCode.INVALID_REQUEST, correlation_id)
        if not isinstance(parsed, dict):
            return ApiContractHandler._error(400, ApiErrorCode.INVALID_REQUEST, correlation_id)
        return parsed

    @staticmethod
    def _correlation_id(headers: Mapping[str, str]) -> str:
        candidate = headers.get("X-Correlation-Id", "")
        return candidate if _SAFE_ID.fullmatch(candidate) else uuid.uuid4().hex

    @staticmethod
    def _audit_id(correlation_id: str) -> str:
        return hashlib.sha256(f"api-v1:{correlation_id}".encode()).hexdigest()[:24]

    @staticmethod
    def _success(status: int, body: Mapping[str, object], correlation_id: str) -> HttpResponse:
        result = dict(body)
        result["correlation_id"] = correlation_id
        result["audit_id"] = ApiContractHandler._audit_id(correlation_id)
        return HttpResponse(
            status,
            {"Content-Type": "application/json", "X-Correlation-Id": correlation_id},
            result,
        )

    @staticmethod
    def _error(status: int, code: ApiErrorCode, correlation_id: str) -> HttpResponse:
        return ApiContractHandler._success(
            status,
            {"error": {"code": code.value, "message": "Request could not be processed"}},
            correlation_id,
        )

    @staticmethod
    def _intake_error(error: IntakeCommandRejected, correlation_id: str) -> HttpResponse:
        mapping = {
            IntakeErrorCode.AUTHORIZATION_DENIED: (403, ApiErrorCode.AUTHORIZATION_DENIED),
            IntakeErrorCode.BRANCH_SCOPE_MISMATCH: (403, ApiErrorCode.BRANCH_SCOPE_MISMATCH),
            IntakeErrorCode.IDEMPOTENCY_CONFLICT: (409, ApiErrorCode.CONFLICT),
            IntakeErrorCode.CONCURRENT_CONFLICT: (409, ApiErrorCode.CONFLICT),
            IntakeErrorCode.DUPLICATE_RIDER_CALL: (409, ApiErrorCode.CONFLICT),
            IntakeErrorCode.INVALID_REQUEST: (400, ApiErrorCode.INVALID_REQUEST),
            IntakeErrorCode.PII_POLICY_VIOLATION: (400, ApiErrorCode.PII_POLICY_VIOLATION),
            IntakeErrorCode.RETRYABLE_PERSISTENCE_FAILURE: (
                503,
                ApiErrorCode.RETRYABLE_FAILURE,
            ),
        }
        status, code = mapping[error.code]
        return ApiContractHandler._error(status, code, correlation_id)

    @staticmethod
    def _string(body: Mapping[str, object], key: str) -> str:
        value = body[key]
        if not isinstance(value, str) or not value:
            raise TypeError(f"{key} must be a non-empty string")
        return value

    @staticmethod
    def _integer(body: Mapping[str, object], key: str) -> int:
        value = body[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{key} must be an integer")
        return value

    @staticmethod
    def _mapping(value: object) -> Mapping[str, object]:
        if not isinstance(value, dict):
            raise TypeError("attributes must be an object")
        return value
