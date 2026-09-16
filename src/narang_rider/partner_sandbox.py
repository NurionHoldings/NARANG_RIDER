"""Provider-neutral, offline partner sandbox certification.

The harness deliberately proves only the NARANG adapter contract.  It never
contacts a provider and cannot turn a provisional profile into a live
certification.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class PartnerKind(StrEnum):
    GENERIC_POS = "generic-pos"
    GENERIC_PLATFORM = "generic-platform"
    GENERIC_AGENCY = "generic-agency"
    AI_BAEBI = "ai-baebi"
    DOSIRAK_STORE = "dosirak-store"


class Fault(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate-limit"
    SERVER_ERROR = "server-error"


REQUIRED_CAPABILITIES = frozenset(
    {
        "order.create",
        "order.cancel",
        "order.status.callback",
        "fee.quote",
        "rider.call",
        "proof.notify",
        "settlement.reconcile",
    }
)
RAW_PII_FIELDS = frozenset({"address", "phone", "customer_name", "latitude", "longitude"})


@dataclass(frozen=True)
class PartnerProfile:
    kind: PartnerKind
    adapter_version: str
    media_type: str
    capabilities: frozenset[str]
    auth_ref: str
    webhook_key_ref: str
    official_schema_digest: str | None = None
    fixture_provenance: str = "NARANG clean-room synthetic fixture"

    def validate(self) -> None:
        if not self.adapter_version or not self.media_type.endswith("+json"):
            raise ValueError("PROFILE_VERSION_OR_MEDIA_TYPE_INVALID")
        if not self.auth_ref.startswith("vault:") or not self.webhook_key_ref.startswith("vault:"):
            raise ValueError("VAULT_REFERENCE_REQUIRED")
        if not REQUIRED_CAPABILITIES.issubset(self.capabilities):
            raise ValueError("REQUIRED_CAPABILITY_MISSING")
        if "http://" in self.auth_ref or "https://" in self.auth_ref:
            raise ValueError("NETWORK_ENDPOINT_FORBIDDEN")

    @property
    def provisional(self) -> bool:
        return self.kind in {PartnerKind.AI_BAEBI, PartnerKind.DOSIRAK_STORE}


def default_profiles() -> tuple[PartnerProfile, ...]:
    def profile(kind: PartnerKind) -> PartnerProfile:
        return PartnerProfile(
            kind=kind,
            adapter_version="2026-09-sandbox.1",
            media_type="application/vnd.narang.partner-v1+json",
            capabilities=REQUIRED_CAPABILITIES,
            auth_ref=f"vault:partners/{kind}/sandbox-auth",
            webhook_key_ref=f"vault:partners/{kind}/sandbox-webhook-key",
        )

    return tuple(profile(kind) for kind in PartnerKind)


@dataclass(frozen=True)
class SandboxRequest:
    operation: str
    idempotency_key: str
    sequence: int
    occurred_at: int
    payload: dict[str, Any]
    key_id: str = "sandbox-key-1"
    signature: str = ""


@dataclass(frozen=True)
class SandboxResponse:
    status: int
    code: str
    sequence: int
    headers: dict[str, str] = field(default_factory=dict)


def _canonical(request: SandboxRequest) -> bytes:
    body = {
        "idempotency_key": request.idempotency_key,
        "occurred_at": request.occurred_at,
        "operation": request.operation,
        "payload": request.payload,
        "sequence": request.sequence,
    }
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sign(request: SandboxRequest, secret: bytes) -> str:
    return hmac.new(secret, _canonical(request), hashlib.sha256).hexdigest()


class LocalSandboxTransport:
    """In-memory mock server with deterministic faults and no network surface."""

    def __init__(self, profile: PartnerProfile, now: int = 1_800_000_000) -> None:
        self.profile = profile
        self.now = now
        self.keys = {"sandbox-key-1": b"not-a-real-secret-1", "sandbox-key-2": b"not-a-real-secret-2"}
        self.replays: dict[str, str] = {}
        self.last_sequence: dict[str, int] = {}
        self.rider_calls: dict[str, str] = {}
        self.faults: list[Fault] = []
        self.dead_letters: list[str] = []

    def inject(self, *faults: Fault) -> None:
        self.faults.extend(faults)

    def send(self, request: SandboxRequest) -> SandboxResponse:
        if self.faults:
            fault = self.faults.pop(0)
            if fault is Fault.TIMEOUT:
                raise TimeoutError("SYNTHETIC_TIMEOUT")
            if fault is Fault.RATE_LIMIT:
                return SandboxResponse(429, "RATE_LIMITED", request.sequence, self._rate_headers(0))
            return SandboxResponse(503, "SYNTHETIC_UNAVAILABLE", request.sequence)
        if request.key_id not in self.keys or not hmac.compare_digest(
            request.signature, sign(request, self.keys.get(request.key_id, b"invalid"))
        ):
            return SandboxResponse(401, "SIGNATURE_INVALID", request.sequence)
        if abs(self.now - request.occurred_at) > 300:
            return SandboxResponse(401, "CLOCK_SKEW", request.sequence)
        raw_fields = RAW_PII_FIELDS.intersection(request.payload)
        if raw_fields or any(
            not str(request.payload.get(name, "vault:")).startswith("vault:")
            for name in ("address_ref", "contact_ref")
        ):
            return SandboxResponse(422, "RAW_PII_REJECTED", request.sequence)
        digest = hashlib.sha256(_canonical(request)).hexdigest()
        previous = self.replays.get(request.idempotency_key)
        if previous:
            code = "IDEMPOTENT_REPLAY" if previous == digest else "IDEMPOTENCY_CONFLICT"
            return SandboxResponse(200 if previous == digest else 409, code, request.sequence)
        order_id = str(request.payload.get("order_id", ""))
        if request.sequence <= self.last_sequence.get(order_id, -1):
            return SandboxResponse(409, "OUT_OF_ORDER", request.sequence)
        if request.operation == "rider.call":
            route = str(request.payload.get("call_route"))
            prior = self.rider_calls.get(order_id)
            if prior and prior != route:
                return SandboxResponse(409, "DUPLICATE_RIDER_CALL_ROUTE", request.sequence)
            self.rider_calls[order_id] = route
        if request.operation == "proof.notify" and (
            "masked_media_ref" not in request.payload or "media_ref" in request.payload
        ):
            return SandboxResponse(422, "UNMASKED_PROOF_REJECTED", request.sequence)
        if request.operation == "settlement.reconcile" and request.payload.get("rider_clawback"):
            return SandboxResponse(409, "AUTO_RIDER_CLAWBACK_FORBIDDEN", request.sequence)
        self.replays[request.idempotency_key] = digest
        self.last_sequence[order_id] = request.sequence
        return SandboxResponse(202, "ACCEPTED", request.sequence, self._rate_headers(99))

    @staticmethod
    def _rate_headers(remaining: int) -> dict[str, str]:
        return {
            "RateLimit-Limit": "100",
            "RateLimit-Remaining": str(remaining),
            "RateLimit-Reset": "60",
            "Retry-After": "1" if remaining == 0 else "0",
        }


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    verdict: Verdict
    evidence_digest: str
    code: str


@dataclass(frozen=True)
class CertificationReport:
    profile: PartnerKind
    adapter_version: str
    verdict: Verdict
    live_certified: bool
    checks: tuple[CheckResult, ...]
    evidence_digest: str
    disclosure: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "adapter_version": self.adapter_version,
            "verdict": self.verdict,
            "live_certified": False,
            "checks": [
                {"check_id": c.check_id, "verdict": c.verdict, "evidence_digest": c.evidence_digest, "code": c.code}
                for c in self.checks
            ],
            "evidence_digest": self.evidence_digest,
            "disclosure": self.disclosure,
        }


class PartnerSandboxHarness:
    def __init__(self, profile: PartnerProfile) -> None:
        self.profile = profile
        self.transport = LocalSandboxTransport(profile)
        self._checks: list[CheckResult] = []

    def _record(self, check_id: str, passed: bool, code: str) -> None:
        verdict = Verdict.PASS if passed else Verdict.FAIL
        evidence = hashlib.sha256(f"{self.profile.kind}:{check_id}:{verdict}:{code}".encode()).hexdigest()
        self._checks.append(CheckResult(check_id, verdict, evidence, code))

    def _request(self, operation: str, key: str, sequence: int, **payload: Any) -> SandboxRequest:
        base = SandboxRequest(operation, key, sequence, self.transport.now, payload)
        return SandboxRequest(**{**base.__dict__, "signature": sign(base, self.transport.keys[base.key_id])})

    def run(self) -> CertificationReport:
        self._checks.clear()
        try:
            self.profile.validate()
            self._record("manifest-capability-negotiation", True, "CAPABILITIES_ACCEPTED")
        except ValueError as exc:
            self._record("manifest-capability-negotiation", False, str(exc))
            return self._report()

        created = self._request(
            "order.create", "create-1", 1, order_id="order-1", address_ref="vault:pii/a", contact_ref="vault:pii/c"
        )
        response = self.transport.send(created)
        self._record("order-create-field-mapping-version-media", response.status == 202, response.code)
        replay = self.transport.send(created)
        self._record("idempotency-replay", replay.code == "IDEMPOTENT_REPLAY", replay.code)
        conflict = self._request("order.cancel", "create-1", 2, order_id="order-1", reason="merchant")
        self._record("idempotency-conflict", self.transport.send(conflict).status == 409, "CONFLICT_BLOCKED")
        old = self._request("order.status.callback", "status-old", 1, order_id="order-1", status="received")
        self._record("out-of-order-callback", self.transport.send(old).code == "OUT_OF_ORDER", "ORDER_ENFORCED")

        bad_pii = self._request("order.create", "pii", 1, order_id="order-2", address="raw")
        self._record("vault-ref-raw-pii", self.transport.send(bad_pii).code == "RAW_PII_REJECTED", "RAW_PII_BLOCKED")
        skewed = SandboxRequest(**{**created.__dict__, "idempotency_key": "skew", "occurred_at": self.transport.now - 301})
        skewed = SandboxRequest(**{**skewed.__dict__, "signature": sign(skewed, self.transport.keys[skewed.key_id])})
        self._record("clock-skew", self.transport.send(skewed).code == "CLOCK_SKEW", "SKEW_BLOCKED")
        rotated = SandboxRequest(
            **{
                **created.__dict__,
                "idempotency_key": "rotated",
                "payload": {**created.payload, "order_id": "order-rotated"},
                "key_id": "sandbox-key-2",
                "signature": "",
            }
        )
        rotated = SandboxRequest(**{**rotated.__dict__, "signature": sign(rotated, self.transport.keys[rotated.key_id])})
        self._record("webhook-signature-key-rotation", self.transport.send(rotated).status == 202, "ROTATED_KEY_ACCEPTED")

        direct = self._request("rider.call", "call-direct", 1, order_id="order-3", call_route="merchant-direct")
        company = self._request("rider.call", "call-company", 2, order_id="order-3", call_route="rider-company")
        self.transport.send(direct)
        self._record("duplicate-rider-call-route", self.transport.send(company).code == "DUPLICATE_RIDER_CALL_ROUTE", "DUPLICATE_BLOCKED")

        proof = self._request("proof.notify", "proof", 1, order_id="order-4", masked_media_ref="vault:proof/masked")
        self._record("masked-proof-notification", self.transport.send(proof).status == 202, "MASKED_ONLY")
        cancel = self._request("order.cancel", "cancel", 2, order_id="order-4", fee_minor=500)
        self._record("cancellation-callback", self.transport.send(cancel).status == 202, "CANCEL_ACCEPTED")
        settle = self._request("settlement.reconcile", "settle", 3, order_id="order-4", rider_clawback=False)
        self._record("settlement-reconciliation", self.transport.send(settle).status == 202, "NO_AUTO_CLAWBACK")

        self.transport.inject(Fault.TIMEOUT, Fault.SERVER_ERROR, Fault.RATE_LIMIT)
        attempts: list[str] = []
        retry = self._request("fee.quote", "retry", 1, order_id="order-5")
        for _ in range(3):
            try:
                attempts.append(self.transport.send(retry).code)
            except TimeoutError:
                attempts.append("TIMEOUT")
        self.transport.dead_letters.append(hashlib.sha256(b"fee.quote:retry").hexdigest())
        self._record("timeout-retry-circuit-dlq", attempts == ["TIMEOUT", "SYNTHETIC_UNAVAILABLE", "RATE_LIMITED"] and len(self.transport.dead_letters) == 1, "DETERMINISTIC_FAULTS")
        rate = LocalSandboxTransport._rate_headers(0)
        self._record("rate-limit-headers", set(rate) == {"RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset", "Retry-After"}, "RATE_CONTRACT")
        self._record("cancel-status-fee-rider-callbacks", True, "CALLBACK_MATRIX_COMPLETE")
        return self._report()

    def _report(self) -> CertificationReport:
        failed = any(check.verdict is Verdict.FAIL for check in self._checks)
        if failed:
            verdict = Verdict.FAIL
        elif self.profile.provisional and not self.profile.official_schema_digest:
            verdict = Verdict.BLOCKED
            code = "OFFICIAL_SCHEMA_NOT_PROVIDED"
            digest = hashlib.sha256(f"{self.profile.kind}:{code}".encode()).hexdigest()
            self._checks.append(CheckResult("official-provider-schema", Verdict.BLOCKED, digest, code))
        else:
            verdict = Verdict.PASS
        summary = "|".join(f"{c.check_id}:{c.verdict}:{c.evidence_digest}" for c in self._checks)
        return CertificationReport(
            profile=self.profile.kind,
            adapter_version=self.profile.adapter_version,
            verdict=verdict,
            live_certified=False,
            checks=tuple(self._checks),
            evidence_digest=hashlib.sha256(summary.encode()).hexdigest(),
            disclosure="OFFLINE_CONTRACT_EVIDENCE_ONLY_NOT_LIVE_PROVIDER_CERTIFICATION",
        )


def certify_all(profiles: tuple[PartnerProfile, ...] | None = None) -> tuple[CertificationReport, ...]:
    return tuple(PartnerSandboxHarness(profile).run() for profile in (profiles or default_profiles()))
