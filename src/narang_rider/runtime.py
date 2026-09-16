"""Dependency-light ASGI runtime shell for NARANG RIDER.

This module assembles already-audited domain boundaries. It does not open a
network listener, fetch keys, connect to databases, or read secrets itself.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .api_contract import ApiContractHandler, HttpRequest, HttpResponse

Scope = Mapping[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_METHOD = re.compile(r"^[A-Z]{3,12}$")
_REDACTED_HEADERS = {"authorization", "cookie", "proxy-authorization", "set-cookie"}
_SINGLETON_HEADERS = {
    "authorization", "content-length", "content-type", "cookie", "host",
    "idempotency-key", "x-branch-id", "x-csrf-token",
}


class StructuredLogger(Protocol):
    def write(self, event: Mapping[str, object]) -> None: ...


class NullLogger:
    def write(self, event: Mapping[str, object]) -> None:
        return None


@dataclass(frozen=True)
class RuntimeConfig:
    database_config_ref: str
    jwks_config_ref: str
    max_body_bytes: int = 65_536
    request_timeout_seconds: float = 5.0
    trusted_proxy_addresses: frozenset[str] = frozenset()

    def validate(self) -> None:
        for value in (self.database_config_ref, self.jwks_config_ref):
            if not value or not value.startswith("config://"):
                raise RuntimeError("required runtime configuration reference is missing")
        if not 1 <= self.max_body_bytes <= 1_048_576:
            raise RuntimeError("invalid request body limit")
        if not 0.1 <= self.request_timeout_seconds <= 30:
            raise RuntimeError("invalid request timeout")


@dataclass(frozen=True)
class RuntimeDependencies:
    handler: ApiContractHandler
    logger: StructuredLogger


def create_runtime(config: RuntimeConfig, dependencies: RuntimeDependencies) -> NarangAsgiApp:
    """Production composition boundary; refuses incomplete configuration."""

    config.validate()
    return NarangAsgiApp(config=config, dependencies=dependencies)


def create_test_runtime(handler: ApiContractHandler, logger: StructuredLogger | None = None) -> NarangAsgiApp:
    """Explicit in-memory test factory; never intended for deployment."""

    return NarangAsgiApp(
        config=RuntimeConfig("config://test/db", "config://test/jwks"),
        dependencies=RuntimeDependencies(handler, logger or NullLogger()),
    )


class NarangAsgiApp:
    def __init__(self, *, config: RuntimeConfig, dependencies: RuntimeDependencies) -> None:
        config.validate()
        self._config = config
        self._dependencies = dependencies
        self._ready = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope_type != "http":
            return
        await self._http(scope, receive, send)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "lifespan.startup":
                self._config.validate()
                self._ready = True
                await send({"type": "lifespan.startup.complete"})
            elif message_type == "lifespan.shutdown":
                self._ready = False
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = time.monotonic()
        try:
            headers = self._headers(scope)
        except InvalidHeaders:
            request_id = uuid.uuid4().hex
            await self._send_response(
                send, self._error(400, "AMBIGUOUS_HEADERS", request_id), request_id
            )
            return
        request_id = self._request_id(headers)
        method = str(scope.get("method", "")).upper()
        path = str(scope.get("path", ""))
        remote_address = self._remote_address(scope, headers)
        status = 500
        try:
            if not _METHOD.fullmatch(method):
                response = self._error(400, "INVALID_METHOD", request_id)
            elif path == "/health/live":
                response = HttpResponse(200, {}, {"status": "ok"})
            elif path == "/health/ready":
                response = HttpResponse(
                    200 if self._ready else 503,
                    {},
                    {"status": "ready" if self._ready else "unavailable"},
                )
            elif method == "OPTIONS":
                response = self._error(405, "METHOD_NOT_ALLOWED", request_id)
            else:
                body = await self._read_body(receive, headers)
                response = self._dependencies.handler.handle(
                    HttpRequest(method=method, path=path, headers=headers, body=body)
                )
            status = response.status
            await self._send_response(send, response, request_id)
        except ClientDisconnected:
            status = 499
        except TimeoutError:
            status = 408
            await self._send_response(
                send, self._error(408, "REQUEST_TIMEOUT", request_id), request_id
            )
        except BodyTooLarge:
            status = 413
            await self._send_response(
                send, self._error(413, "PAYLOAD_TOO_LARGE", request_id), request_id
            )
        except asyncio.CancelledError:
            raise
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            status = 500
            await self._send_response(
                send, self._error(500, "INTERNAL_ERROR", request_id), request_id
            )
        finally:
            self._dependencies.logger.write(
                {
                    "event": "http_request",
                    "method": method,
                    "path": path,
                    "remote_address": remote_address,
                    "request_id": request_id,
                    "status": status,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                }
            )

    async def _read_body(self, receive: Receive, headers: Mapping[str, str]) -> bytes:
        content_length = headers.get("Content-Length")
        if content_length is not None:
            try:
                length = int(content_length)
            except ValueError as error:
                raise BodyTooLarge from error
            if length < 0 or length > self._config.max_body_bytes:
                raise BodyTooLarge
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await asyncio.wait_for(
                receive(), timeout=self._config.request_timeout_seconds
            )
            message_type = message.get("type")
            if message_type == "http.disconnect":
                raise ClientDisconnected
            if message_type != "http.request":
                raise ClientDisconnected
            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes):
                raise ClientDisconnected
            size += len(chunk)
            if size > self._config.max_body_bytes:
                raise BodyTooLarge
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)

    @staticmethod
    def _headers(scope: Scope) -> dict[str, str]:
        result: dict[str, str] = {}
        for raw_name, raw_value in scope.get("headers", []):
            try:
                name = raw_name.decode("latin-1")
                value = raw_value.decode("latin-1")
            except (AttributeError, UnicodeDecodeError):
                continue
            canonical = "-".join(piece.capitalize() for piece in name.split("-"))
            lowered = canonical.lower()
            if lowered in _SINGLETON_HEADERS and canonical in result:
                raise InvalidHeaders
            if lowered in _REDACTED_HEADERS or canonical not in result:
                result[canonical] = value
        return result

    def _remote_address(self, scope: Scope, headers: Mapping[str, str]) -> str:
        client = scope.get("client")
        peer = str(client[0]) if isinstance(client, (list, tuple)) and client else "unknown"
        if peer not in self._config.trusted_proxy_addresses:
            return peer
        forwarded = headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        return forwarded or peer

    @staticmethod
    def _request_id(headers: Mapping[str, str]) -> str:
        supplied = headers.get("X-Request-Id", "")
        return supplied if _REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex

    @staticmethod
    def _error(status: int, code: str, request_id: str) -> HttpResponse:
        return HttpResponse(status, {}, {"error": {"code": code}, "request_id": request_id})

    @staticmethod
    async def _send_response(send: Send, response: HttpResponse, request_id: str) -> None:
        payload = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode()
        headers = {
            "cache-control": "no-store",
            "content-length": str(len(payload)),
            "content-type": "application/json; charset=utf-8",
            "referrer-policy": "no-referrer",
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "x-request-id": request_id,
        }
        for name, value in response.headers.items():
            if name.lower() not in {"access-control-allow-origin", "set-cookie"}:
                headers[name.lower()] = value
        await send(
            {
                "type": "http.response.start",
                "status": response.status,
                "headers": [(name.encode(), value.encode()) for name, value in headers.items()],
            }
        )
        await send({"type": "http.response.body", "body": payload})


class BodyTooLarge(RuntimeError):
    pass


class ClientDisconnected(RuntimeError):
    pass


class InvalidHeaders(RuntimeError):
    pass
