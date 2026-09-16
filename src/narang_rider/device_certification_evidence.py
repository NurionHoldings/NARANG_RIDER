"""Signed, privacy-minimized device certification evidence intake."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from hmac import compare_digest
from hmac import new as hmac_new


class EvidenceRejected(ValueError):
    pass


class EvidenceStatus(StrEnum):
    HUMAN_REVIEW = "human_review"
    BLOCKED = "blocked"


REQUIRED_SCENARIOS = frozenset(
    {
        "installed_launch",
        "app_unavailable",
        "update_required",
        "os_restricted",
        "offline",
        "return_state_single_use",
        "accessibility",
    }
)

FORBIDDEN_FIELDS = frozenset(
    {
        "device_serial",
        "advertising_id",
        "imei",
        "phone_number",
        "rider_id",
        "order_id",
        "address",
        "latitude",
        "longitude",
        "api_key",
        "access_token",
    }
)


@dataclass(frozen=True)
class DeviceEvidenceEnvelope:
    schema_version: str
    evidence_id: str
    provider_id: str
    platform: str
    app_build: str
    os_family: str
    device_class_hash: str
    verifier_key_id: str
    nonce: str
    issued_at: datetime
    expires_at: datetime
    scenario_results: tuple[tuple[str, bool], ...]
    metadata_fields: frozenset[str]
    payload_digest: str
    signature: str

    def canonical_payload(self) -> str:
        results = ",".join(
            f"{name}:{str(passed).lower()}" for name, passed in sorted(self.scenario_results)
        )
        fields = ",".join(sorted(self.metadata_fields))
        parts = (
            self.schema_version,
            self.evidence_id,
            self.provider_id,
            self.platform,
            self.app_build,
            self.os_family,
            self.device_class_hash,
            self.verifier_key_id,
            self.nonce,
            self.issued_at.isoformat(),
            self.expires_at.isoformat(),
            results,
            fields,
        )
        return "|".join(parts)


@dataclass(frozen=True)
class DeviceEvidenceReceipt:
    evidence_id: str
    provider_id: str
    platform: str
    status: EvidenceStatus
    passed_scenarios: tuple[str, ...]
    failed_scenarios: tuple[str, ...]
    receipt_digest: str
    can_mark_provider_verified: bool = False
    production_activation_allowed: bool = False

    def as_ci_artifact(self) -> dict[str, object]:
        return {"schema": "narang.device-certification-receipt.v1", **self.__dict__}


class DeviceEvidenceVerifier:
    def __init__(self, verifier_keys: dict[str, bytes]) -> None:
        self._keys = dict(verifier_keys)
        self._consumed_nonces: set[str] = set()
        self._accepted_evidence_ids: set[str] = set()

    def receive(
        self, envelope: DeviceEvidenceEnvelope, *, now: datetime
    ) -> DeviceEvidenceReceipt:
        self._validate_identity(envelope)
        if now >= envelope.expires_at or envelope.issued_at > now:
            raise EvidenceRejected("evidence expired or issued in the future")
        if envelope.expires_at - envelope.issued_at > timedelta(hours=1):
            raise EvidenceRejected("evidence validity exceeds one hour")
        if envelope.nonce in self._consumed_nonces:
            raise EvidenceRejected("evidence nonce replay")
        if envelope.evidence_id in self._accepted_evidence_ids:
            raise EvidenceRejected("duplicate evidence identity")

        key = self._keys.get(envelope.verifier_key_id)
        if key is None:
            raise EvidenceRejected("unregistered verifier key")
        canonical = envelope.canonical_payload()
        calculated_digest = sha256(canonical.encode()).hexdigest()
        if not compare_digest(calculated_digest, envelope.payload_digest):
            raise EvidenceRejected("payload digest mismatch")
        calculated_signature = hmac_new(key, calculated_digest.encode(), sha256).hexdigest()
        if not compare_digest(calculated_signature, envelope.signature):
            raise EvidenceRejected("signature verification failed")

        names = [name for name, _passed in envelope.scenario_results]
        if len(names) != len(set(names)) or not REQUIRED_SCENARIOS.issubset(names):
            raise EvidenceRejected("complete unique scenario coverage required")
        if envelope.metadata_fields & FORBIDDEN_FIELDS:
            raise EvidenceRejected("forbidden device, identity, location, or credential field")

        self._consumed_nonces.add(envelope.nonce)
        self._accepted_evidence_ids.add(envelope.evidence_id)
        passed = tuple(sorted(name for name, result in envelope.scenario_results if result))
        failed = tuple(sorted(name for name, result in envelope.scenario_results if not result))
        status = EvidenceStatus.HUMAN_REVIEW if not failed else EvidenceStatus.BLOCKED
        receipt_digest = sha256(
            f"{envelope.evidence_id}|{status.value}|{','.join(passed)}|{','.join(failed)}".encode()
        ).hexdigest()
        return DeviceEvidenceReceipt(
            evidence_id=envelope.evidence_id,
            provider_id=envelope.provider_id,
            platform=envelope.platform,
            status=status,
            passed_scenarios=passed,
            failed_scenarios=failed,
            receipt_digest=receipt_digest,
        )

    @staticmethod
    def _validate_identity(envelope: DeviceEvidenceEnvelope) -> None:
        if envelope.schema_version != "narang.device-certification-evidence.v1":
            raise EvidenceRejected("unsupported evidence schema")
        values = (
            envelope.evidence_id,
            envelope.provider_id,
            envelope.platform,
            envelope.app_build,
            envelope.os_family,
            envelope.verifier_key_id,
            envelope.nonce,
        )
        if any(not value or any(ord(character) < 32 for character in value) for value in values):
            raise EvidenceRejected("invalid evidence identity or scope")
        if (
            len(envelope.device_class_hash) != 64
            or len(envelope.payload_digest) != 64
            or len(envelope.signature) != 64
        ):
            raise EvidenceRejected("hashed identifiers, digest, and signature must be SHA-256")


def sign_synthetic_envelope(
    envelope: DeviceEvidenceEnvelope, synthetic_key: bytes
) -> DeviceEvidenceEnvelope:
    """Test helper; production signing is intentionally outside this service."""
    digest = sha256(envelope.canonical_payload().encode()).hexdigest()
    signature = hmac_new(synthetic_key, digest.encode(), sha256).hexdigest()
    return DeviceEvidenceEnvelope(
        **{
            **envelope.__dict__,
            "payload_digest": digest,
            "signature": signature,
        }
    )
