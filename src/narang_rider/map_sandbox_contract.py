"""Offline contract verification for synthetic map-provider adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any, Protocol
from urllib.parse import urlsplit


class ContractRejected(ValueError):
    pass


class FailureMode(StrEnum):
    NONE = "none"
    TIMEOUT = "timeout"
    QUOTA_EXHAUSTED = "quota_exhausted"
    KEY_REVOKED = "key_revoked"
    MALFORMED_RESPONSE = "malformed_response"


@dataclass(frozen=True)
class SandboxContract:
    provider_id: str
    fixture_version: str
    allowed_capability: str
    allowed_host: str
    allowed_path: str
    allowed_request_fields: frozenset[str]
    required_response_fields: frozenset[str]
    maximum_requests: int
    evidence_digest: str
    synthetic_only: bool = True

    def validate(self) -> None:
        if not self.synthetic_only:
            raise ContractRejected("only synthetic sandbox contracts are allowed")
        if not self.provider_id or not self.fixture_version:
            raise ContractRejected("contract identity required")
        if not self.allowed_host or self.allowed_host.endswith(".invalid") is False:
            raise ContractRejected("reserved .invalid sandbox host required")
        if not self.allowed_path.startswith("/") or ".." in self.allowed_path:
            raise ContractRejected("safe absolute sandbox path required")
        if self.maximum_requests <= 0:
            raise ContractRejected("positive request budget required")
        if len(self.evidence_digest) != 64:
            raise ContractRejected("SHA-256 evidence digest required")


@dataclass(frozen=True)
class SyntheticRequest:
    capability: str
    url: str
    fields: dict[str, Any]
    credential_ref: str


@dataclass(frozen=True)
class SyntheticResponse:
    status_code: int
    fields: dict[str, Any]
    failure_mode: FailureMode = FailureMode.NONE


class SyntheticAdapter(Protocol):
    def invoke(self, request: SyntheticRequest) -> SyntheticResponse: ...


@dataclass(frozen=True)
class ContractReport:
    provider_id: str
    fixture_version: str
    passed: bool
    violations: tuple[str, ...]
    requests_used: int
    credential_exposed: bool
    personal_data_exposed: bool
    production_activation_allowed: bool
    report_digest: str

    def as_ci_artifact(self) -> dict[str, object]:
        return {"schema": "narang.map-sandbox-contract.v1", **self.__dict__}


class MapSandboxContractRunner:
    FORBIDDEN_FIELDS = frozenset(
        {
            "rider_id",
            "order_id",
            "customer_name",
            "phone",
            "address",
            "precise_location",
            "api_key",
            "access_token",
        }
    )

    def verify(
        self,
        *,
        contract: SandboxContract,
        adapter: SyntheticAdapter,
        request: SyntheticRequest,
    ) -> ContractReport:
        contract.validate()
        violations: list[str] = []
        parsed = urlsplit(request.url)
        if request.capability != contract.allowed_capability:
            violations.append("capability_not_allowlisted")
        if (
            parsed.scheme != "https"
            or parsed.netloc != contract.allowed_host
            or parsed.path != contract.allowed_path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            violations.append("endpoint_not_allowlisted")

        request_fields = set(request.fields)
        if not request_fields.issubset(contract.allowed_request_fields):
            violations.append("request_field_not_allowlisted")
        personal_data_exposed = bool(request_fields & self.FORBIDDEN_FIELDS)
        if personal_data_exposed:
            violations.append("personal_data_exposed")
        credential_exposed = (
            not request.credential_ref.startswith("vault-ref:synthetic:")
            or any(key in repr(request.fields).casefold() for key in ("api_key", "access_token"))
        )
        if credential_exposed:
            violations.append("credential_boundary_failed")

        requests_used = 0
        response: SyntheticResponse | None = None
        if not violations:
            requests_used = 1
            response = adapter.invoke(request)
            if requests_used > contract.maximum_requests:
                violations.append("quota_budget_exceeded")

        if response is not None:
            if response.failure_mode is not FailureMode.NONE:
                violations.append(f"provider_{response.failure_mode.value}")
            if response.status_code != 200:
                violations.append("unexpected_status")
            response_fields = set(response.fields)
            if not contract.required_response_fields.issubset(response_fields):
                violations.append("response_schema_mismatch")
            if response_fields & self.FORBIDDEN_FIELDS:
                violations.append("response_personal_data_exposed")
                personal_data_exposed = True

        canonical = "|".join(
            (
                contract.provider_id,
                contract.fixture_version,
                ",".join(sorted(violations)),
                str(requests_used),
                str(credential_exposed),
                str(personal_data_exposed),
            )
        )
        return ContractReport(
            provider_id=contract.provider_id,
            fixture_version=contract.fixture_version,
            passed=not violations,
            violations=tuple(violations),
            requests_used=requests_used,
            credential_exposed=credential_exposed,
            personal_data_exposed=personal_data_exposed,
            production_activation_allowed=False,
            report_digest=sha256(canonical.encode()).hexdigest(),
        )


def sandbox_release_gate(report: ContractReport) -> dict[str, object]:
    return {
        "contract_passed": report.passed,
        "external_gate": "EXT-02_PENDING",
        "production_status": "BLOCKED",
        "operator_approval_required": True,
        "runtime_activation_allowed": False,
    }
