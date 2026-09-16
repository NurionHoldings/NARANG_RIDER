from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.external_intake import (
    EvidenceIntake,
    EvidenceSubmission,
    IntakeRejected,
    IntakeStatus,
    ProviderKind,
    inspect_text_for_forbidden_data,
    operator_checklist,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def evidence(**changes):
    values = {
        "evidence_id": "ev-001",
        "provider_kind": ProviderKind.AI_BAEBI,
        "provider_identity_ref": "provider-registry://ai-baebi",
        "source_url": "https://developer.example.test/openapi.json",
        "document_sha256": "a" * 64,
        "issued_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=30),
        "environment": "sandbox",
        "media_type": "application/json",
        "malware_scan_ref": "malware-scan://scan-001",
        "schema_version": "2026-09",
        "schema_compatible": True,
    }
    values.update(changes)
    return EvidenceSubmission(**values)


def test_full_certification_requires_harness_and_two_humans():
    intake = EvidenceIntake(now=NOW)
    assert intake.receive(evidence()).status is IntakeStatus.RECEIVED
    assert intake.quarantine("ev-001", scan_passed=True).status is IntakeStatus.QUARANTINED
    checked = intake.verify(
        "ev-001",
        verifier_refs=("review://contract-001",),
        official_source_confirmed=True,
    )
    assert checked.status is IntakeStatus.VERIFIED
    with pytest.raises(IntakeRejected):
        intake.certify(
            "ev-001", harness_passed=True, approval_roles=frozenset({"OPERATOR"})
        )
    certified = intake.certify(
        "ev-001",
        harness_passed=True,
        approval_roles=frozenset({"ETHERNian", "OPERATOR"}),
    )
    assert certified.status is IntakeStatus.CERTIFIED


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"environment": "production"}, "sandbox"),
        ({"provider_identity_ref": "ai-baebi"}, "registry"),
        ({"document_sha256": "not-a-digest"}, "SHA-256"),
        ({"media_type": "application/zip"}, "file type"),
        ({"contains_embedded_secrets": True}, "secrets"),
        ({"contains_personal_data": True}, "personal data"),
        ({"source_url": "https://user:secret@example.test/spec"}, "credential-free"),
    ],
)
def test_receive_is_fail_closed(changes, message):
    with pytest.raises(IntakeRejected, match=message):
        EvidenceIntake(now=NOW).receive(evidence(**changes))


def test_upload_does_not_auto_trust_and_bad_scan_blocks():
    intake = EvidenceIntake(now=NOW)
    intake.receive(evidence())
    assert intake.get("ev-001").status is IntakeStatus.RECEIVED
    assert intake.quarantine("ev-001", scan_passed=False).status is IntakeStatus.BLOCKED
    with pytest.raises(IntakeRejected):
        intake.certify(
            "ev-001",
            harness_passed=True,
            approval_roles=frozenset({"ETHERNian", "OPERATOR"}),
        )


def test_incompatible_schema_is_blocked_after_quarantine():
    intake = EvidenceIntake(now=NOW)
    intake.receive(evidence(schema_compatible=False))
    intake.quarantine("ev-001", scan_passed=True)
    result = intake.verify(
        "ev-001",
        verifier_refs=("review://schema-001",),
        official_source_confirmed=True,
    )
    assert result.status is IntakeStatus.BLOCKED
    assert result.block_reasons == ("schema incompatible",)


def test_idempotency_rejects_changed_metadata():
    intake = EvidenceIntake(now=NOW)
    first = intake.receive(evidence())
    assert intake.receive(evidence()) is first
    with pytest.raises(IntakeRejected, match="reuse"):
        intake.receive(evidence(source_url="https://other.example.test/spec"))


def test_preflight_and_all_provider_checklists():
    assert inspect_text_for_forbidden_data("api_key=real-secret")
    assert inspect_text_for_forbidden_data("010-1234-5678")
    for kind in ProviderKind:
        checklist = operator_checklist(kind)
        assert len(checklist["required_materials"]) == 9
        assert checklist["release_status"] == "BLOCKED"
