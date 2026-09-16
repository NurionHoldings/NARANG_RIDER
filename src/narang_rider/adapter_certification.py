from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AdapterCapability(StrEnum):
    ORDER_CREATE = "ORDER_CREATE"
    STATUS_CALLBACK = "STATUS_CALLBACK"
    CANCEL = "CANCEL"
    REDISPATCH = "REDISPATCH"
    FEE_QUOTE = "FEE_QUOTE"
    IDEMPOTENCY = "IDEMPOTENCY"


@dataclass(frozen=True)
class AdapterManifest:
    adapter_id: str
    provider: str
    version: str
    capabilities: frozenset[AdapterCapability]
    auth_reference: str
    callback_url_reference: str

    def __post_init__(self) -> None:
        if not self.auth_reference.startswith("vault:"):
            raise ValueError("ADAPTER_AUTH_VAULT_REQUIRED")
        if not self.callback_url_reference.startswith("vault:"):
            raise ValueError("CALLBACK_REFERENCE_VAULT_REQUIRED")


@dataclass(frozen=True)
class CertificationEvidence:
    manifest: AdapterManifest
    order_create_passed: bool
    status_callback_passed: bool
    duplicate_replay_passed: bool
    conflicting_replay_blocked: bool
    cancellation_passed: bool
    fee_quote_passed: bool
    pii_minimization_passed: bool


@dataclass(frozen=True)
class CertifiedAdapter:
    adapter_id: str
    version: str
    enabled_capabilities: frozenset[AdapterCapability]
    certified: bool
    failure_codes: tuple[str, ...]


class AdapterCertificationService:
    def __init__(self) -> None:
        self._certified: dict[tuple[str, str], CertifiedAdapter] = {}

    def certify(self, evidence: CertificationEvidence) -> CertifiedAdapter:
        required = {
            AdapterCapability.ORDER_CREATE,
            AdapterCapability.STATUS_CALLBACK,
            AdapterCapability.IDEMPOTENCY,
        }
        failures = []
        if not required.issubset(evidence.manifest.capabilities):
            failures.append("REQUIRED_CAPABILITY_MISSING")
        checks = {
            "ORDER_CREATE_FAILED": evidence.order_create_passed,
            "STATUS_CALLBACK_FAILED": evidence.status_callback_passed,
            "DUPLICATE_REPLAY_FAILED": evidence.duplicate_replay_passed,
            "CONFLICTING_REPLAY_NOT_BLOCKED": evidence.conflicting_replay_blocked,
            "PII_MINIMIZATION_FAILED": evidence.pii_minimization_passed,
        }
        if AdapterCapability.CANCEL in evidence.manifest.capabilities:
            checks["CANCELLATION_FAILED"] = evidence.cancellation_passed
        if AdapterCapability.FEE_QUOTE in evidence.manifest.capabilities:
            checks["FEE_QUOTE_FAILED"] = evidence.fee_quote_passed
        failures.extend(code for code, passed in checks.items() if not passed)
        result = CertifiedAdapter(
            evidence.manifest.adapter_id,
            evidence.manifest.version,
            evidence.manifest.capabilities if not failures else frozenset(),
            not failures,
            tuple(sorted(failures)),
        )
        self._certified[(result.adapter_id, result.version)] = result
        return result

    def require(self, adapter_id: str, version: str, capability: AdapterCapability) -> None:
        try:
            adapter = self._certified[(adapter_id, version)]
        except KeyError as exc:
            raise ValueError("ADAPTER_NOT_CERTIFIED") from exc
        if not adapter.certified or capability not in adapter.enabled_capabilities:
            raise ValueError("ADAPTER_CAPABILITY_NOT_CERTIFIED")
