"""Deterministic OpenAPI 3.1 contract assembled without generator dependencies."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

MUTATIONS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
RAW_PII_NAMES = frozenset({"address", "delivery_address", "phone", "phone_number", "email", "name"})
SAFE_EXAMPLE = {"branch_id": "branch-synthetic-01", "vault_ref": "vault://synthetic/ref-001"}


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    path: str
    tag: str
    request_schema: str | None = None
    response_schema: str = "OperationResult"
    provisional: bool = False


OPERATIONS = (
    Operation("createOrder", "POST", "/api/v1/orders", "orders", "OrderIntake"),
    Operation("callRider", "POST", "/api/v1/orders/{order_id}/rider-calls", "orders", "RiderCall"),
    Operation("getOrderStatus", "GET", "/api/v1/orders/{order_id}/status", "orders"),
    Operation("listMerchantOrders", "GET", "/api/v1/merchant/orders", "merchant"),
    Operation("createMerchantOrder", "POST", "/api/v1/merchant/orders", "merchant", "OrderIntake"),
    Operation(
        "confirmMerchantQuote",
        "POST",
        "/api/v1/merchant/orders/{order_id}/quote-confirmations",
        "merchant",
        "Command",
    ),
    Operation(
        "submitMerchantOrder",
        "POST",
        "/api/v1/merchant/orders/{order_id}/submissions",
        "merchant",
        "Command",
    ),
    Operation(
        "recordPackaging",
        "PUT",
        "/api/v1/merchant/orders/{order_id}/packaging",
        "merchant",
        "Packaging",
    ),
    Operation(
        "cancelMerchantOrder",
        "POST",
        "/api/v1/merchant/orders/{order_id}/cancellations",
        "merchant",
        "Command",
    ),
    Operation("getMerchantOrder", "GET", "/api/v1/merchant/orders/{order_id}", "merchant"),
    Operation("setRiderAvailability", "PUT", "/api/v1/rider/availability", "rider", "Command"),
    Operation("listRiderOffers", "GET", "/api/v1/rider/offers", "rider"),
    Operation(
        "respondRiderOffer", "POST", "/api/v1/rider/offers/{offer_id}/responses", "rider", "Command"
    ),
    Operation(
        "recordRiderProgress",
        "POST",
        "/api/v1/rider/assignments/{assignment_id}/progress",
        "rider",
        "Command",
    ),
    Operation(
        "issueRiderProofGrant",
        "POST",
        "/api/v1/rider/assignments/{assignment_id}/proof-grants",
        "rider",
        "Command",
    ),
    Operation(
        "completeRiderDelivery",
        "POST",
        "/api/v1/rider/assignments/{assignment_id}/delivery",
        "rider",
        "ProofReceipt",
    ),
    Operation(
        "getRiderEarnings", "GET", "/api/v1/rider/assignments/{assignment_id}/earnings", "rider"
    ),
    Operation(
        "getCustomerProof", "GET", "/api/v1/customer/orders/{order_id}/delivery-proof", "customer"
    ),
    Operation(
        "issueExteriorReportGrant",
        "POST",
        "/api/v1/customer/orders/{order_id}/exterior-report-grants",
        "customer",
        "Command",
    ),
    Operation(
        "createExteriorReport",
        "POST",
        "/api/v1/customer/orders/{order_id}/exterior-reports",
        "customer",
        "ProofReceipt",
    ),
    Operation(
        "getExteriorReport",
        "GET",
        "/api/v1/customer/orders/{order_id}/exterior-reports",
        "customer",
    ),
    Operation("listBranches", "GET", "/api/v1/control/branches", "control"),
    Operation(
        "activateBranch",
        "POST",
        "/api/v1/control/branches/{branch_id}/activate",
        "control",
        "Command",
    ),
    Operation(
        "getBranchDashboard", "GET", "/api/v1/control/branches/{branch_id}/dashboard", "control"
    ),
    Operation(
        "overrideBranchPolicy",
        "POST",
        "/api/v1/control/branches/{branch_id}/policies",
        "control",
        "Command",
    ),
    Operation("createOperatorCommand", "POST", "/api/v1/control/commands", "control", "Command"),
    Operation(
        "approveOperatorCommand",
        "POST",
        "/api/v1/control/commands/{command_id}/approvals",
        "control",
        "Approval",
    ),
    Operation(
        "transferCorridorOrder",
        "POST",
        "/api/v1/control/corridors/{corridor_id}/transfers",
        "control",
        "Approval",
    ),
    Operation(
        "getSettlementStatement",
        "GET",
        "/api/v1/settlements/statements/{statement_id}",
        "settlement",
    ),
    Operation(
        "disputeSettlement",
        "POST",
        "/api/v1/settlements/statements/{statement_id}/disputes",
        "settlement",
        "Command",
    ),
    Operation(
        "createPayoutInstruction",
        "POST",
        "/api/v1/settlements/statements/{statement_id}/payout-instructions",
        "settlement",
        "Command",
    ),
    Operation(
        "approvePayoutInstruction",
        "POST",
        "/api/v1/payout-instructions/{instruction_id}/approvals",
        "settlement",
        "Approval",
    ),
    Operation(
        "getPayoutInstruction", "GET", "/api/v1/payout-instructions/{instruction_id}", "settlement"
    ),
    Operation(
        "createRouteQuote",
        "POST",
        "/api/v1/orders/{order_id}/route-quotes",
        "geospatial",
        "VaultLocationRefs",
    ),
    Operation(
        "startLocationSession",
        "POST",
        "/api/v1/orders/{order_id}/location-sessions",
        "geospatial",
        "VaultLocationRefs",
    ),
    Operation("getCustomerProgress", "GET", "/api/v1/orders/{order_id}/progress", "geospatial"),
    Operation(
        "stopLocationSession",
        "POST",
        "/api/v1/orders/{order_id}/location-sessions/{session_id}/stop",
        "geospatial",
        "Command",
    ),
    Operation("createPrivacyRequest", "POST", "/api/v1/privacy/requests", "privacy", "Command"),
    Operation("getPrivacyRequest", "GET", "/api/v1/privacy/requests/{request_id}", "privacy"),
    Operation(
        "verifyPrivacyRequest",
        "POST",
        "/api/v1/privacy/requests/{request_id}/verify",
        "privacy",
        "Command",
    ),
    Operation(
        "actOnPrivacyRequest",
        "POST",
        "/api/v1/privacy/requests/{request_id}/actions",
        "privacy",
        "Command",
    ),
    Operation(
        "exportPrivacyRequest", "GET", "/api/v1/privacy/requests/{request_id}/export", "privacy"
    ),
    Operation(
        "withdrawPrivacyConsent",
        "POST",
        "/api/v1/privacy/consents/{consent_id}/withdraw",
        "privacy",
        "Command",
    ),
    Operation("listPrivacyPolicies", "GET", "/api/v1/privacy/policies", "privacy"),
    Operation("createLegalHold", "POST", "/api/v1/privacy-ops/holds", "privacy", "Approval"),
    Operation("planPrivacySweep", "POST", "/api/v1/privacy-ops/sweeps", "privacy", "Command"),
    Operation(
        "executePrivacySweep",
        "POST",
        "/api/v1/privacy-ops/sweeps/{sweep_id}/execute",
        "privacy",
        "Approval",
    ),
    Operation("createSupportCase", "POST", "/api/v1/support/cases", "support", "Command"),
    Operation("listSupportCases", "GET", "/api/v1/support/cases", "support"),
    Operation("getSupportCase", "GET", "/api/v1/support/cases/{case_id}", "support"),
    Operation(
        "appendSupportMessage",
        "POST",
        "/api/v1/support/cases/{case_id}/messages",
        "support",
        "Command",
    ),
    Operation(
        "appendSupportEvidence",
        "POST",
        "/api/v1/support/cases/{case_id}/evidence",
        "support",
        "ProofReceipt",
    ),
    Operation(
        "appealSupportCase", "POST", "/api/v1/support/cases/{case_id}/appeals", "support", "Command"
    ),
    Operation(
        "assignSupportCase",
        "POST",
        "/api/v1/support-ops/cases/{case_id}/assign",
        "support",
        "Command",
    ),
    Operation(
        "transitionSupportCase",
        "POST",
        "/api/v1/support-ops/cases/{case_id}/transition",
        "support",
        "Command",
    ),
    Operation(
        "actOnSupportCase",
        "POST",
        "/api/v1/support-ops/cases/{case_id}/actions",
        "support",
        "Approval",
    ),
    Operation(
        "submitPilotReadiness",
        "POST",
        "/api/v1/admin/branches/{branch_id}/pilot/readiness",
        "pilot",
        "Command",
    ),
    Operation(
        "getPilotChecklist", "GET", "/api/v1/admin/branches/{branch_id}/pilot/checklist", "pilot"
    ),
    Operation(
        "containPilot",
        "POST",
        "/api/v1/admin/branches/{branch_id}/pilot/contain",
        "pilot",
        "Command",
    ),
    Operation(
        "restartPilot",
        "POST",
        "/api/v1/admin/branches/{branch_id}/pilot/restart",
        "pilot",
        "Approval",
    ),
    Operation(
        "createPartnerSandboxOrder",
        "POST",
        "/api/v1/provisional/partners/{partner_id}/orders",
        "partners",
        "OrderIntake",
        provisional=True,
    ),
)


def _schema(
    properties: dict[str, dict[str, Any]], required: tuple[str, ...] = ()
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        result["required"] = list(required)
    return result


SCHEMAS = {
    "Command": _schema({"reason_code": {"type": "string", "maxLength": 128}}),
    "Approval": _schema({"decision": {"type": "string", "enum": ["approve", "reject"]}}),
    "OrderIntake": _schema(
        {
            "merchant_id": {"type": "string"},
            "delivery_address_vault_ref": {"type": "string", "pattern": "^vault://"},
            "recipient_phone_vault_ref": {"type": "string", "pattern": "^vault://"},
        },
        ("merchant_id", "delivery_address_vault_ref", "recipient_phone_vault_ref"),
    ),
    "RiderCall": _schema(
        {"route": {"type": "string", "enum": ["merchant_direct", "rider_company"]}}
    ),
    "Packaging": _schema(
        {
            "double_packaging": {"type": "string", "enum": ["true", "false", "not_applicable"]},
            "seal_number": {"type": "string"},
        }
    ),
    "ProofReceipt": _schema(
        {
            "sanitized_media_vault_ref": {"type": "string", "pattern": "^vault://"},
            "nonce": {"type": "string"},
        }
    ),
    "VaultLocationRefs": _schema(
        {
            "origin_vault_ref": {"type": "string", "pattern": "^vault://"},
            "destination_vault_ref": {"type": "string", "pattern": "^vault://"},
        }
    ),
    "OperationResult": _schema(
        {
            "status": {"type": "string"},
            "correlation_id": {"type": "string"},
            "replayed": {"type": "boolean"},
        },
        ("status", "correlation_id"),
    ),
    "Error": _schema(
        {
            "code": {"type": "string"},
            "message": {"type": "string"},
            "correlation_id": {"type": "string"},
            "retryable": {"type": "boolean"},
        },
        ("code", "correlation_id", "retryable"),
    ),
}


def build_openapi(operations: tuple[Operation, ...] = OPERATIONS) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    seen_operation_ids: set[str] = set()
    seen_routes: set[tuple[str, str]] = set()
    for operation in sorted(operations, key=lambda item: (item.path, item.method)):
        route_key = (operation.path, operation.method)
        if operation.operation_id in seen_operation_ids or route_key in seen_routes:
            raise ValueError("duplicate path/method or operationId")
        seen_operation_ids.add(operation.operation_id)
        seen_routes.add(route_key)
        mutation = operation.method in MUTATIONS
        parameters = [
            {"$ref": "#/components/parameters/CorrelationId"},
            {"$ref": "#/components/parameters/BranchId"},
        ]
        if mutation:
            parameters.extend(
                (
                    {"$ref": "#/components/parameters/CsrfToken"},
                    {"$ref": "#/components/parameters/IdempotencyKey"},
                )
            )
        item: dict[str, Any] = {
            "operationId": operation.operation_id,
            "tags": [operation.tag],
            "security": [{"cookieSession": [], "csrfToken": []}]
            if mutation
            else [{"cookieSession": []}],
            "parameters": parameters,
            "responses": {
                "200": {
                    "description": "Success",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{operation.response_schema}"},
                            "example": SAFE_EXAMPLE,
                        }
                    },
                },
                "default": {
                    "description": "Stable error envelope",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}
                    },
                },
            },
            "x-rate-limit-policy": "branch-and-principal",
            "x-branch-scoped": True,
        }
        if operation.request_schema:
            item["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{operation.request_schema}"}
                    }
                },
            }
        if operation.provisional:
            item["x-availability"] = "provisional-sandbox-only"
            item["description"] = (
                "No live certification; official partner contract and credentials required."
            )
        paths.setdefault(operation.path, {})[operation.method.lower()] = item
    document = {
        "openapi": "3.1.0",
        "info": {
            "title": "NARANG RIDER API",
            "version": "0.1.0-rc.3",
            "description": "Synthetic sandbox contract; not live certification.",
        },
        "servers": [
            {"url": "https://sandbox.invalid", "description": "Non-routable synthetic example"}
        ],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "cookieSession": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": "__Host-narang_session",
                },
                "csrfToken": {"type": "apiKey", "in": "header", "name": "X-CSRF-Token"},
            },
            "parameters": {
                "CorrelationId": {
                    "name": "X-Correlation-Id",
                    "in": "header",
                    "required": False,
                    "schema": {"type": "string", "maxLength": 128},
                },
                "BranchId": {
                    "name": "X-Branch-Id",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,128}$"},
                },
                "CsrfToken": {
                    "name": "X-CSRF-Token",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string"},
                },
                "IdempotencyKey": {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "maxLength": 128},
                },
            },
            "schemas": copy.deepcopy(SCHEMAS),
        },
    }
    validate_openapi(document)
    return document


def validate_openapi(document: dict[str, Any]) -> None:
    seen_ids: set[str] = set()
    seen_routes: set[tuple[str, str]] = set()
    for path, path_item in document.get("paths", {}).items():
        for method, operation in path_item.items():
            key = (path, method.upper())
            operation_id = operation.get("operationId")
            if key in seen_routes or not operation_id or operation_id in seen_ids:
                raise ValueError("duplicate path/method or operationId")
            seen_routes.add(key)
            seen_ids.add(operation_id)
            if not operation.get("security"):
                raise ValueError(f"protected operation lacks authentication: {operation_id}")
            refs = {item.get("$ref", "") for item in operation.get("parameters", [])}
            if (
                method.upper() in MUTATIONS
                and not {
                    "#/components/parameters/CsrfToken",
                    "#/components/parameters/IdempotencyKey",
                }
                <= refs
            ):
                raise ValueError(f"mutation lacks CSRF/idempotency: {operation_id}")
            if "default" not in operation.get("responses", {}):
                raise ValueError(f"operation lacks stable error envelope: {operation_id}")
    for name, schema in document.get("components", {}).get("schemas", {}).items():
        for field in schema.get("properties", {}):
            normalized = re.sub(r"[^a-z_]", "", field.lower())
            if normalized in RAW_PII_NAMES or normalized.endswith("_raw"):
                raise ValueError(f"raw PII schema field forbidden: {name}.{field}")
    serialized = json.dumps(document, ensure_ascii=False)
    if re.search(r"01\d[- ]?\d{3,4}[- ]?\d{4}|(?:lat|lng)\s*[:=]", serialized, re.IGNORECASE):
        raise ValueError("unsafe personal data in example")


def canonical_bytes(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def contract_digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(document)).hexdigest()
