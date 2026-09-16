import json

import pytest

from narang_rider.api_contract import (
    MAX_REQUEST_BYTES,
    ROUTE_MANIFEST,
    ApiContractHandler,
    ApiErrorCode,
    FakeAuthContextVerifier,
    HttpRequest,
    VerifiedPrincipal,
)
from narang_rider.application import CommandCapability, OrderIntakeApplicationService
from narang_rider.persistence import InMemoryPersistence, RecordKind, canonical_payload_digest

BRANCH = "branch-sejong-01"
TOKEN = "Bearer verified-token"


class ToggleRateLimit:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[str] = []

    def allow(self, principal: VerifiedPrincipal, route_id: str) -> bool:
        self.calls.append(route_id)
        return self.allowed


@pytest.fixture
def system():
    store = InMemoryPersistence()
    rate_limit = ToggleRateLimit()
    principal = VerifiedPrincipal(
        actor_id="merchant-operator-1",
        branch_id=BRANCH,
        capabilities=frozenset({CommandCapability.INGEST_ORDER, CommandCapability.CALL_RIDER}),
    )
    handler = ApiContractHandler(
        auth_verifier=FakeAuthContextVerifier({TOKEN: principal}),
        rate_limit=rate_limit,
        intake=OrderIntakeApplicationService(store),
        repository=store,
        unit_of_work=store,
    )
    return handler, store, rate_limit


def headers(**extra: str) -> dict[str, str]:
    result = {
        "Authorization": TOKEN,
        "Content-Type": "application/json",
        "X-Branch-Id": BRANCH,
        "X-Correlation-Id": "corr-001",
        "Idempotency-Key": "idem-001",
    }
    result.update(extra)
    return result


def intake_body(**extra: object) -> bytes:
    body = {
        "source_system": "dosirak.store",
        "source_order_id": "external-1",
        "merchant_id": "merchant-1",
        "total_won": 18_000,
        "delivery_address_vault_ref": "vault://pii/address/1",
        "recipient_phone_vault_ref": "vault://pii/phone/1",
        "rider_call_route": "merchant_direct",
    }
    body.update(extra)
    return json.dumps(body).encode()


def post_order(handler: ApiContractHandler, **extra: object):
    return handler.handle(HttpRequest("POST", "/api/v1/orders", headers(), intake_body(**extra)))


def test_route_manifest_is_versioned_and_machine_readable() -> None:
    assert {route.route_id for route in ROUTE_MANIFEST} == {
        "order_intake",
        "rider_call",
        "order_status",
    }
    assert all(route.path_template.startswith("/api/v1/") for route in ROUTE_MANIFEST)
    assert {route.response_dto for route in ROUTE_MANIFEST} == {
        "OrderAcceptedV1",
        "RiderCallAcceptedV1",
        "OrderStatusV1",
    }


def test_order_intake_contract_returns_correlation_and_audit_ids(system) -> None:
    handler, store, rate_limit = system
    response = post_order(handler)

    assert response.status == 201
    assert response.body["status"] == "RECEIVED"
    assert response.body["correlation_id"] == "corr-001"
    assert len(response.body["audit_id"]) == 24
    assert response.headers["X-Correlation-Id"] == "corr-001"
    assert rate_limit.calls == ["order_intake"]
    assert store.get(RecordKind.ORDER, BRANCH, response.body["order_id"]) is not None


def test_status_query_reads_only_verified_branch(system) -> None:
    handler, _, _ = system
    created = post_order(handler)
    response = handler.handle(
        HttpRequest(
            "GET",
            f"/api/v1/orders/{created.body['order_id']}/status",
            headers(),
        )
    )
    assert response.status == 200
    assert response.body["status"] == "RECEIVED"
    assert response.body["version"] == 1


def test_arbitrary_role_and_capability_headers_are_not_trusted(system) -> None:
    handler, _, _ = system
    request_headers = headers(
        Authorization="Bearer attacker",
        **{"X-Role": "superadmin", "X-Capabilities": "*"},
    )
    response = handler.handle(HttpRequest("POST", "/api/v1/orders", request_headers, intake_body()))
    assert response.status == 401
    assert response.body["error"]["code"] == ApiErrorCode.AUTHENTICATION_REQUIRED


def test_verified_principal_cannot_cross_branch_via_header(system) -> None:
    handler, _, _ = system
    response = handler.handle(
        HttpRequest(
            "POST",
            "/api/v1/orders",
            headers(**{"X-Branch-Id": "branch-busan-01"}),
            intake_body(),
        )
    )
    assert response.status == 401
    assert response.body["error"]["code"] == ApiErrorCode.AUTHENTICATION_REQUIRED


def test_idempotency_header_is_required_for_mutation(system) -> None:
    handler, _, _ = system
    request_headers = headers()
    request_headers.pop("Idempotency-Key")
    response = handler.handle(HttpRequest("POST", "/api/v1/orders", request_headers, intake_body()))
    assert response.status == 400
    assert response.body["error"]["code"] == ApiErrorCode.IDEMPOTENCY_REQUIRED


def test_retry_and_conflicting_payload_have_stable_http_results(system) -> None:
    handler, _, _ = system
    assert post_order(handler).status == 201
    replay = post_order(handler)
    conflict = post_order(handler, total_won=99_000)
    assert replay.status == 201
    assert replay.body["replayed"] is True
    assert conflict.status == 409
    assert conflict.body["error"]["code"] == ApiErrorCode.CONFLICT


@pytest.mark.parametrize(
    "body",
    [
        {"address": "세종시 어느로 1"},
        {"phone": "010-0000-0000"},
        {"recipient_name": "홍길동"},
    ],
)
def test_raw_pii_fields_are_rejected_without_echo(system, body) -> None:
    handler, _, _ = system
    response = post_order(handler, **body)
    assert response.status == 400
    serialized = json.dumps(response.body, ensure_ascii=False)
    assert next(iter(body.values())) not in serialized
    assert "traceback" not in serialized.lower()


def test_nested_raw_pii_is_mapped_without_internal_details(system) -> None:
    handler, _, _ = system
    response = post_order(handler, attributes={"customer": {"phone": "010-0000-0000"}})
    assert response.status == 400
    assert response.body["error"] == {
        "code": ApiErrorCode.PII_POLICY_VIOLATION,
        "message": "Request could not be processed",
    }
    assert "phone" not in json.dumps(response.body)


def test_content_type_json_and_body_size_are_enforced(system) -> None:
    handler, _, _ = system
    wrong_type = handler.handle(
        HttpRequest(
            "POST",
            "/api/v1/orders",
            headers(**{"Content-Type": "text/plain"}),
            intake_body(),
        )
    )
    oversized = handler.handle(
        HttpRequest(
            "POST",
            "/api/v1/orders",
            headers(),
            b"x" * (MAX_REQUEST_BYTES + 1),
        )
    )
    assert wrong_type.status == 415
    assert oversized.status == 413


@pytest.mark.parametrize("body", [b"not-json", b"[]", b'{"source_system": 1}'])
def test_schema_and_json_shape_are_validated(system, body) -> None:
    handler, _, _ = system
    response = handler.handle(HttpRequest("POST", "/api/v1/orders", headers(), body))
    assert response.status == 400
    assert response.body["error"]["code"] == ApiErrorCode.INVALID_REQUEST


def test_rate_limit_hook_runs_before_body_processing(system) -> None:
    handler, _, rate_limit = system
    rate_limit.allowed = False
    response = handler.handle(HttpRequest("POST", "/api/v1/orders", headers(), b"not-json"))
    assert response.status == 429
    assert rate_limit.calls == ["order_intake"]


def test_missing_capability_is_forbidden() -> None:
    store = InMemoryPersistence()
    principal = VerifiedPrincipal("actor", BRANCH, frozenset({CommandCapability.CALL_RIDER}))
    handler = ApiContractHandler(
        auth_verifier=FakeAuthContextVerifier({TOKEN: principal}),
        rate_limit=ToggleRateLimit(),
        intake=OrderIntakeApplicationService(store),
        repository=store,
        unit_of_work=store,
    )
    response = post_order(handler)
    assert response.status == 403
    assert response.body["error"]["code"] == ApiErrorCode.AUTHORIZATION_DENIED


def test_standalone_rider_call_contract_and_duplicate_blocking(system) -> None:
    handler, store, _ = system
    order_id = "ord_existing"
    unit = store.begin(
        branch_id=BRANCH,
        idempotency_key="seed-order",
        payload_digest=canonical_payload_digest({"order_id": order_id}),
    )
    unit.put(
        RecordKind.ORDER,
        order_id,
        {"branch_id": BRANCH, "state": "RECEIVED"},
        expected_version=0,
    )
    unit.commit()
    request = HttpRequest(
        "POST",
        f"/api/v1/orders/{order_id}/rider-calls",
        headers(),
        json.dumps({"route": "rider_company"}).encode(),
    )
    first = handler.handle(request)
    replay = handler.handle(request)
    attack = handler.handle(
        HttpRequest(
            "POST",
            f"/api/v1/orders/{order_id}/rider-calls",
            headers(),
            json.dumps({"route": "merchant_direct"}).encode(),
        )
    )
    assert first.status == 201
    assert replay.body["replayed"] is True
    assert attack.status == 409
    assert store.get(RecordKind.RIDER_CALL, BRANCH, order_id).payload["route"] == "rider_company"


def test_unknown_route_has_stable_not_found_body(system) -> None:
    handler, _, _ = system
    response = handler.handle(HttpRequest("GET", "/api/v2/orders", headers()))
    assert response.status == 404
    assert response.body["error"]["code"] == ApiErrorCode.ROUTE_NOT_FOUND
