from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.privacy import *


NOW = datetime(2026, 9, 16, tzinfo=UTC)


def subject(branch="sejong", who="customer-1"):
    return PrivacyPrincipal(who, who, "customer", branch)


def officer(branch="sejong", who="officer-1"):
    return PrivacyPrincipal(who, None, "privacy_officer", branch, True)


def service(right=RightType.EXPORT):
    svc = PrivacyLifecycleService(default_policy_registry(), response_days=21)
    req = svc.submit(principal=subject(), request_id="req-1", branch_id="sejong", right=right,
                     identity_proof_ref="vault://identity/proof-123", idempotency_key="idem-1", now=NOW)
    return svc, req


def record(record_id="r1", subject_id="customer-1", category=DataCategory.CONTACT):
    return SubjectDataRef(record_id, subject_id, "sejong", category, "vault://pii/ref-123",
                          {"status": "active", "category": category.value}, NOW - timedelta(days=4000))


def test_inventory_requires_every_category_and_raw_pii_is_rejected():
    assert set(DataCategory) == {p for p in DataCategory}
    with pytest.raises(ValueError):
        PolicyRegistry([])
    with pytest.raises(ValueError, match="raw PII"):
        SubjectDataRef("r", "s", "b", DataCategory.CONTACT, "vault://x/12345",
                       {"phone": "010-0000-0000"}, NOW)


def test_consent_version_and_withdrawal_are_immutable_and_idempotent():
    consent = ConsentRecord("c1", "s1", "marketing", "v3", NOW)
    withdrawn = consent.withdraw(at=NOW + timedelta(hours=1))
    assert consent.active and not withdrawn.active
    assert withdrawn.withdraw(at=NOW + timedelta(days=1)) == withdrawn


def test_dsar_is_owned_branch_scoped_deadlined_and_idempotent():
    svc, req = service()
    replay = svc.submit(principal=subject(), request_id="different", branch_id="sejong",
                        right=RightType.EXPORT, identity_proof_ref="vault://identity/proof-123",
                        idempotency_key="idem-1", now=NOW)
    assert replay is req and req.due_at == NOW + timedelta(days=21)
    with pytest.raises(PrivacyRejected) as error:
        svc.submit(principal=subject("other"), request_id="x", branch_id="sejong",
                   right=RightType.ACCESS, identity_proof_ref="vault://identity/proof-123",
                   idempotency_key="x", now=NOW)
    assert error.value.code is PrivacyErrorCode.FORBIDDEN


def test_forged_identity_and_idor_fail_closed():
    svc = PrivacyLifecycleService(default_policy_registry())
    with pytest.raises(PrivacyRejected) as error:
        svc.submit(principal=PrivacyPrincipal("attacker", "victim", "customer", "sejong"),
                   request_id="x", branch_id="sejong", right=RightType.ACCESS,
                   identity_proof_ref="raw-proof", idempotency_key="x", now=NOW)
    assert error.value.code is PrivacyErrorCode.FORBIDDEN


def test_transition_denial_requires_human_policy_reason_and_arkaon_cannot_deny():
    svc, req = service()
    svc.transition(req.request_id, RequestState.IDENTITY_VERIFIED, actor=officer())
    svc.transition(req.request_id, RequestState.IN_REVIEW, actor=officer())
    with pytest.raises(PrivacyRejected) as error:
        svc.transition(req.request_id, RequestState.HUMAN_DENIED, actor=officer(),
                       reason_ref="policy://exception/123", arkaon_initiated=True)
    assert error.value.code is PrivacyErrorCode.AI_AUTHORITY_FORBIDDEN


def test_export_is_minimized_signed_and_rejects_mixed_subject_or_bulk():
    svc, req = service()
    manifest = svc.export(req.request_id, [record()], actor=officer(), signing_key=b"test-key")
    assert manifest.digest and manifest.record_refs == ("r1",)
    assert "vault" not in str(manifest.minimized_records)
    with pytest.raises(PrivacyRejected) as error:
        svc.export(req.request_id, [record(subject_id="other")], actor=officer(), signing_key=b"k")
    assert error.value.code is PrivacyErrorCode.MIXED_SUBJECT


def test_deletion_emits_vault_tombstone_but_ledger_audit_are_unlinked():
    svc, req = service(RightType.DELETION)
    intents = svc.deletion_intents(req.request_id,
        [record("contact"), record("audit", category=DataCategory.AUDIT)], [],
        actor=officer(), now=NOW)
    assert intents[0].kind == "vault_tombstone_and_metadata_anonymize"
    assert intents[1].kind == "pseudonymize_and_unlink"
    assert svc.deletion_intents(req.request_id,
        [record("contact")], [], actor=officer(), now=NOW)[0] == intents[0]


def test_legal_hold_is_scoped_expiring_and_dual_approved():
    hold = PrivacyLifecycleService.create_hold(hold_id="h1", subject_id="s1", record_ids=["r1"],
        reason_ref="legal://case/123", expires_at=NOW + timedelta(days=30),
        approvers=("legal-1", "privacy-2"), now=NOW)
    assert hold.active(NOW) and not hold.active(NOW + timedelta(days=31))
    for ids, approvers in [([], ("a", "b")), (["r"], ("a", "a"))]:
        with pytest.raises(PrivacyRejected):
            PrivacyLifecycleService.create_hold(hold_id="h", subject_id="s", record_ids=ids,
                reason_ref="legal://case/123", expires_at=NOW + timedelta(days=1),
                approvers=approvers, now=NOW)


def test_expired_hold_does_not_evade_deletion():
    svc, req = service(RightType.DELETION)
    expired = LegalHold("h", "customer-1", frozenset({"r1"}), "legal://case/1",
                        NOW - timedelta(seconds=1), ("a", "b"))
    assert svc.deletion_intents(req.request_id, [record()], [expired], actor=officer(), now=NOW)


def test_sweeper_requires_dry_run_review_execute_and_is_idempotent():
    svc, _ = service()
    sweep = svc.dry_run_sweep("sw1", [record()], [], now=NOW)
    with pytest.raises(PrivacyRejected):
        svc.execute_sweep(sweep)
    svc.review_sweep(sweep, reviewer_id="reviewer")
    first = svc.execute_sweep(sweep)
    assert first.phase is SweepPhase.EXECUTED and first.executed_record_ids == ("r1",)
    assert svc.execute_sweep(sweep) is first


def test_vendor_cross_border_needs_assessment_and_expiry():
    invalid = VendorTransferApproval("v1", "register://vendors/v1", "JP", None,
                                     "approval://privacy/1", NOW + timedelta(days=1))
    with pytest.raises(PrivacyRejected):
        PrivacyLifecycleService.validate_vendor(invalid, now=NOW)
    valid = VendorTransferApproval("v1", "register://vendors/v1", "JP",
        "assessment://transfer/1", "approval://privacy/1", NOW + timedelta(days=1))
    PrivacyLifecycleService.validate_vendor(valid, now=NOW)


def test_breach_clock_preserves_human_legal_assessment():
    case = PrivacyLifecycleService.open_breach_case("b1", ["vault://evidence/123"], now=NOW)
    assert case.assessment_due_at == NOW + timedelta(hours=24)
    assert case.legal_conclusion is None and case.status == "human_assessment_required"


def test_route_manifest_exposes_subject_and_ops_boundaries():
    assert any("/privacy/requests" in route for route in PRIVACY_ROUTE_MANIFEST)
    assert any("/privacy-ops/holds" in route for route in PRIVACY_ROUTE_MANIFEST)
