from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from narang_rider.openapi_contract import Operation, build_openapi, validate_openapi

ROOT = Path(__file__).resolve().parents[1]


def test_generated_openapi_is_deterministic_and_checked_in() -> None:
    expected = json.loads((ROOT / "api/openapi.json").read_text(encoding="utf-8"))
    assert build_openapi() == expected
    assert expected["openapi"] == "3.1.0"
    assert expected["info"]["description"].endswith("not live certification.")


def test_duplicate_operation_or_route_is_rejected() -> None:
    duplicate_id = (
        Operation("same", "GET", "/first", "test"),
        Operation("same", "GET", "/second", "test"),
    )
    with pytest.raises(ValueError, match="duplicate"):
        build_openapi(duplicate_id)
    duplicate_route = (
        Operation("first", "GET", "/same", "test"),
        Operation("second", "GET", "/same", "test"),
    )
    with pytest.raises(ValueError, match="duplicate"):
        build_openapi(duplicate_route)


def test_missing_auth_and_mutation_guards_are_rejected() -> None:
    document = build_openapi()
    operation = document["paths"]["/api/v1/orders"]["post"]
    operation["security"] = []
    with pytest.raises(ValueError, match="authentication"):
        validate_openapi(document)

    document = build_openapi()
    operation = document["paths"]["/api/v1/orders"]["post"]
    operation["parameters"] = [
        item for item in operation["parameters"] if "IdempotencyKey" not in item["$ref"]
    ]
    with pytest.raises(ValueError, match="CSRF/idempotency"):
        validate_openapi(document)


def test_raw_pii_schema_and_unsafe_examples_are_rejected() -> None:
    document = build_openapi()
    document["components"]["schemas"]["Bad"] = {
        "type": "object",
        "properties": {"phone": {"type": "string"}},
    }
    with pytest.raises(ValueError, match="raw PII"):
        validate_openapi(document)

    document = build_openapi()
    document["paths"]["/api/v1/orders"]["post"]["responses"]["200"]["content"]["application/json"][
        "example"
    ] = {"contact": "010-1234-5678"}
    with pytest.raises(ValueError, match="unsafe personal data"):
        validate_openapi(document)


def test_frontend_generated_operations_exactly_match_spec_and_routes_are_declared() -> None:
    document = build_openapi()
    expected = {
        (operation["operationId"], method.upper(), path)
        for path, path_item in document["paths"].items()
        for method, operation in path_item.items()
    }
    generated = (ROOT / "frontend/src/openapi-contract.ts").read_text(encoding="utf-8")
    actual = set(
        re.findall(r'^  (\w+): \{ method: "(\w+)", path: "([^"]+)" \},$', generated, re.MULTILINE)
    )
    assert actual == expected

    api_client = (ROOT / "frontend/src/api.ts").read_text(encoding="utf-8")
    client_paths = set(re.findall(r': "(/api/v1/[^"]+)"', api_client))
    spec_paths = set(document["paths"])
    assert client_paths <= spec_paths


def test_error_envelope_and_provisional_boundary_are_explicit() -> None:
    document = build_openapi()
    for path_item in document["paths"].values():
        for operation in path_item.values():
            assert operation["responses"]["default"]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/Error"
            }
    provisional = document["paths"]["/api/v1/provisional/partners/{partner_id}/orders"]["post"]
    assert provisional["x-availability"] == "provisional-sandbox-only"
    assert "official partner contract" in provisional["description"]
