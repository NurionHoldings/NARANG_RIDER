from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Mapping

import pytest

from narang_rider.api_contract import HttpResponse
from narang_rider.runtime import (
    RuntimeConfig,
    RuntimeDependencies,
    create_runtime,
    create_test_runtime,
)


def async_test(function):
    @functools.wraps(function)
    def wrapped():
        asyncio.run(function())

    return wrapped


class Handler:
    def __init__(self, response: HttpResponse | None = None, error: Exception | None = None) -> None:
        self.response = response or HttpResponse(200, {}, {"accepted": True})
        self.error = error
        self.requests = []

    def handle(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.response


class Logger:
    def __init__(self) -> None:
        self.events: list[Mapping[str, object]] = []

    def write(self, event: Mapping[str, object]) -> None:
        self.events.append(dict(event))


def scope(
    *, method: str = "POST", path: str = "/api/v1/orders",
    headers: list[tuple[bytes, bytes]] | None = None, client: str = "10.0.0.5",
):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "client": (client, 1234),
    }


async def invoke(app, request_scope, messages):
    pending = list(messages)
    sent = []

    async def receive():
        return pending.pop(0)

    async def send(message):
        sent.append(message)

    await app(request_scope, receive, send)
    return sent


def response(sent):
    start = next(item for item in sent if item["type"] == "http.response.start")
    body = next(item for item in sent if item["type"] == "http.response.body")
    return start, json.loads(body["body"])


@async_test
async def test_partial_body_is_joined_and_security_headers_are_added() -> None:
    handler = Handler()
    app = create_test_runtime(handler)
    sent = await invoke(
        app,
        scope(headers=[(b"x-request-id", b"request-1")]),
        [
            {"type": "http.request", "body": b'{"a":', "more_body": True},
            {"type": "http.request", "body": b"1}", "more_body": False},
        ],
    )
    start, _ = response(sent)
    headers = dict(start["headers"])
    assert handler.requests[0].body == b'{"a":1}'
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-frame-options"] == b"DENY"
    assert b"access-control-allow-origin" not in headers


@async_test
async def test_oversized_stream_and_declared_length_are_rejected() -> None:
    app = create_runtime(
        RuntimeConfig("config://db", "config://jwks", max_body_bytes=4),
        RuntimeDependencies(Handler(), Logger()),
    )
    streamed = await invoke(
        app, scope(), [{"type": "http.request", "body": b"12345", "more_body": False}]
    )
    assert response(streamed)[0]["status"] == 413
    declared = await invoke(
        app,
        scope(headers=[(b"content-length", b"5")]),
        [{"type": "http.request", "body": b"", "more_body": False}],
    )
    assert response(declared)[0]["status"] == 413


@async_test
async def test_disconnect_does_not_attempt_response_and_is_logged() -> None:
    logger = Logger()
    app = create_test_runtime(Handler(), logger)
    sent = await invoke(app, scope(), [{"type": "http.disconnect"}])
    assert sent == []
    assert logger.events[-1]["status"] == 499


@async_test
async def test_forwarded_address_is_ignored_without_trusted_peer() -> None:
    logger = Logger()
    config = RuntimeConfig("config://db", "config://jwks", trusted_proxy_addresses=frozenset({"proxy"}))
    app = create_runtime(config, RuntimeDependencies(Handler(), logger))
    messages = [{"type": "http.request", "body": b"", "more_body": False}]
    await invoke(app, scope(headers=[(b"x-forwarded-for", b"198.51.100.9")]), messages)
    assert logger.events[-1]["remote_address"] == "10.0.0.5"
    await invoke(
        app,
        scope(client="proxy", headers=[(b"x-forwarded-for", b"198.51.100.9")]),
        messages,
    )
    assert logger.events[-1]["remote_address"] == "198.51.100.9"


@async_test
async def test_health_endpoints_are_minimal_and_readiness_tracks_lifespan() -> None:
    app = create_test_runtime(Handler())
    unavailable = await invoke(app, scope(method="GET", path="/health/ready"), [])
    assert response(unavailable) == (
        next(item for item in unavailable if item["type"] == "http.response.start"),
        {"status": "unavailable"},
    )
    assert response(unavailable)[0]["status"] == 503

    inputs = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    outputs = []

    async def receive():
        return inputs.pop(0)

    async def send(message):
        outputs.append(message)

    await app({"type": "lifespan"}, receive, send)
    assert outputs == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]


@async_test
async def test_auth_rejection_from_handler_is_preserved() -> None:
    handler = Handler(HttpResponse(401, {}, {"error": {"code": "AUTHENTICATION_REQUIRED"}}))
    sent = await invoke(
        create_test_runtime(handler),
        scope(),
        [{"type": "http.request", "body": b"", "more_body": False}],
    )
    assert response(sent)[0]["status"] == 401


@async_test
async def test_unhandled_exception_is_redacted_from_response_and_log() -> None:
    secret = "database-password-should-never-leak"
    logger = Logger()
    app = create_test_runtime(Handler(error=RuntimeError(secret)), logger)
    sent = await invoke(
        app, scope(), [{"type": "http.request", "body": b"", "more_body": False}]
    )
    _, body = response(sent)
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert secret not in json.dumps(body)
    assert secret not in json.dumps(logger.events)


@async_test
async def test_timeout_options_and_invalid_method_fail_closed() -> None:
    config = RuntimeConfig("config://db", "config://jwks", request_timeout_seconds=0.1)
    app = create_runtime(config, RuntimeDependencies(Handler(), Logger()))

    async def slow_receive():
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b""}

    sent = []

    async def send(message):
        sent.append(message)

    await app(scope(), slow_receive, send)
    assert response(sent)[0]["status"] == 408
    options = await invoke(app, scope(method="OPTIONS"), [])
    assert response(options)[0]["status"] == 405
    invalid = await invoke(app, scope(method="G ET"), [])
    assert response(invalid)[0]["status"] == 400


def test_startup_refuses_missing_database_or_jwks_config_references() -> None:
    dependencies = RuntimeDependencies(Handler(), Logger())
    with pytest.raises(RuntimeError):
        create_runtime(RuntimeConfig("", "config://jwks"), dependencies)
    with pytest.raises(RuntimeError):
        create_runtime(RuntimeConfig("config://db", "raw-secret"), dependencies)
