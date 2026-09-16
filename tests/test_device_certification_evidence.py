from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from narang_rider.device_certification_evidence import (
    REQUIRED_SCENARIOS,
    DeviceEvidenceEnvelope,
    DeviceEvidenceVerifier,
    EvidenceRejected,
    EvidenceStatus,
    sign_synthetic_envelope,
)

NOW = datetime(2026, 9, 16, 13, tzinfo=UTC)
KEY = b"synthetic-verifier-key-not-for-production"


def unsigned(**changes):
    values = {
        "schema_version": "narang.device-certification-evidence.v1",
        "evidence_id": "evidence-1",
        "provider_id": "synthetic-map",
        "platform": "android",
        "app_build": "synthetic-1",
        "os_family": "android-test",
        "device_class_hash": sha256(b"synthetic-device-class").hexdigest(),
        "verifier_key_id": "synthetic-key-1",
        "nonce": "nonce-1",
        "issued_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=30),
        "scenario_results": tuple((name, True) for name in sorted(REQUIRED_SCENARIOS)),
        "metadata_fields": frozenset({"screen_class", "locale", "network_class"}),
        "payload_digest": "0" * 64,
        "signature": "0" * 64,
    }
    values.update(changes)
    return DeviceEvidenceEnvelope(**values)


def envelope(**changes):
    return sign_synthetic_envelope(unsigned(**changes), KEY)


def verifier():
    return DeviceEvidenceVerifier({"synthetic-key-1": KEY})


def test_complete_signed_result_requires_human_review_and_never_promotes():
    receipt = verifier().receive(envelope(), now=NOW)
    assert receipt.status is EvidenceStatus.HUMAN_REVIEW
    assert not receipt.can_mark_provider_verified
    assert not receipt.production_activation_allowed
    assert receipt.as_ci_artifact()["schema"] == "narang.device-certification-receipt.v1"


def test_failed_scenario_is_blocked():
    results = tuple(
        (name, name != "accessibility") for name in sorted(REQUIRED_SCENARIOS)
    )
    receipt = verifier().receive(envelope(scenario_results=results), now=NOW)
    assert receipt.status is EvidenceStatus.BLOCKED
    assert receipt.failed_scenarios == ("accessibility",)


@pytest.mark.parametrize("missing", sorted(REQUIRED_SCENARIOS))
def test_every_required_scenario_is_mandatory(missing):
    results = tuple((name, True) for name in sorted(REQUIRED_SCENARIOS - {missing}))
    with pytest.raises(EvidenceRejected, match="scenario"):
        verifier().receive(envelope(scenario_results=results), now=NOW)


def test_duplicate_scenario_is_rejected():
    results = tuple((name, True) for name in sorted(REQUIRED_SCENARIOS)) + (
        ("offline", True),
    )
    with pytest.raises(EvidenceRejected, match="scenario"):
        verifier().receive(envelope(scenario_results=results), now=NOW)


@pytest.mark.parametrize(
    "field",
    ["device_serial", "advertising_id", "imei", "rider_id", "address", "latitude", "api_key"],
)
def test_direct_identifiers_location_and_credentials_are_rejected(field):
    with pytest.raises(EvidenceRejected, match="forbidden"):
        verifier().receive(envelope(metadata_fields=frozenset({field})), now=NOW)


def test_payload_tampering_and_bad_signature_are_rejected():
    signed = envelope()
    with pytest.raises(EvidenceRejected, match="digest"):
        verifier().receive(replace(signed, app_build="tampered"), now=NOW)
    with pytest.raises(EvidenceRejected, match="signature"):
        verifier().receive(replace(signed, signature="f" * 64), now=NOW)


def test_unknown_verifier_key_is_rejected():
    with pytest.raises(EvidenceRejected, match="unregistered"):
        verifier().receive(envelope(verifier_key_id="unknown"), now=NOW)


def test_nonce_replay_and_duplicate_evidence_are_rejected():
    service = verifier()
    service.receive(envelope(), now=NOW)
    with pytest.raises(EvidenceRejected, match="replay"):
        service.receive(envelope(evidence_id="evidence-2"), now=NOW)
    with pytest.raises(EvidenceRejected, match="duplicate"):
        service.receive(envelope(nonce="nonce-2"), now=NOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"expires_at": NOW},
        {"issued_at": NOW + timedelta(seconds=1)},
        {"expires_at": NOW + timedelta(hours=2)},
    ],
)
def test_expired_future_and_overlong_evidence_are_rejected(changes):
    with pytest.raises(EvidenceRejected):
        verifier().receive(envelope(**changes), now=NOW)


def test_schema_control_char_and_raw_device_identifier_shape_are_rejected():
    candidates = (
        envelope(schema_version="v2"),
        envelope(provider_id="map\nforged"),
        envelope(device_class_hash="raw-device-model"),
    )
    for candidate in candidates:
        with pytest.raises(EvidenceRejected):
            verifier().receive(candidate, now=NOW)


def test_receipt_is_deterministic_without_device_identity():
    first = verifier().receive(envelope(), now=NOW)
    second = verifier().receive(
        envelope(evidence_id="evidence-2", nonce="nonce-2"), now=NOW
    )
    assert first.receipt_digest != second.receipt_digest
    serialized = repr(first)
    assert "synthetic-device-class" not in serialized
    assert "device_class_hash" not in serialized
