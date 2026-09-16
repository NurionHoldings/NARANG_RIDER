from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.evidence import (
    CUSTOMER_COMPLAINT_PURPOSE,
    DELIVERY_EVIDENCE_PURPOSE,
    ComplaintDisposition,
    DeliveryEvidenceBundle,
    EvidenceMethod,
    EvidenceStage,
    UploadInspection,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def bundle() -> DeliveryEvidenceBundle:
    return DeliveryEvidenceBundle(
        grant_ttl=timedelta(minutes=5),
        normal_retention=timedelta(days=7),
        signing_key=b"test-only-signing-key-is-at-least-32-bytes",
    )


def inspection(**changes) -> UploadInspection:
    values = {
        "sha256": "a" * 64,
        "mime_type": "image/jpeg",
        "size_bytes": 100_000,
        "exif_removed": True,
        "malware_clean": True,
        "privacy_mask_applied": False,
        "original_discarded_after_masking": True,
        "fixed_guide_frame_used": True,
        "realtime_prohibited_content_scan_passed": True,
        "depicts_package_at_designated_location": True,
        "captured_in_app_camera": True,
        "gallery_upload": False,
        "server_nonce_visible": True,
        "burst_frame_sha256": ("a" * 64, "b" * 64),
        "frame_consistency_digest": "c" * 64,
        "capture_purpose": DELIVERY_EVIDENCE_PURPOSE,
        "exterior_appears_normal": True,
        "seal_appears_intact": True,
    }
    values.update(changes)
    return UploadInspection(**values)


def issue(service: DeliveryEvidenceBundle):
    return service.issue_grant(
        order_id="order-1", assignment_id="assignment-1", rider_id="rider-1", now=NOW
    )


def record(service: DeliveryEvidenceBundle, grant_id: str, token: str, **changes):
    values = {
        "grant_id": grant_id,
        "token": token,
        "order_id": "order-1",
        "assignment_id": "assignment-1",
        "rider_id": "rider-1",
        "stage": EvidenceStage.DELIVERY_LOCATION,
        "inspection": inspection(),
        "now": NOW + timedelta(minutes=1),
        "chain_refs": ("merchant-seal:123", "pickup-check:456"),
        "customer_notice_sent": True,
        "approximate_delivery_zone": "sejong-grid-12",
        "seal_number": "seal-123",
    }
    values.update(changes)
    return service.record_photo(**values)


def test_one_time_bound_grant_creates_context_only_append_only_receipt() -> None:
    service = bundle()
    grant, token = issue(service)

    receipt = record(service, grant.grant_id, token)

    assert receipt.order_id == "order-1"
    assert receipt.chain_refs == ("merchant-seal:123", "pickup-check:456")
    assert receipt.liability_effect == "CONTEXT_ONLY_HUMAN_REVIEW_REQUIRED"
    assert receipt.penalty_allowed is False
    assert receipt.reusable_for_ai_or_marketing is False
    assert service.verify_receipt_signature(receipt)
    notice = service.customer_notice(receipt.evidence_id)
    assert notice.dispute_route == "IN_APP_REALTIME_COMPLAINT"
    assert not hasattr(notice, "rider_id")
    assert not hasattr(notice, "approximate_delivery_zone")
    with pytest.raises(ValueError, match="INVALID_OR_USED"):
        record(service, grant.grant_id, token, inspection=inspection(sha256="b" * 64))


@pytest.mark.parametrize(
    "changes,error",
    (
        ({"order_id": "other"}, "BINDING_MISMATCH"),
        ({"rider_id": "other"}, "BINDING_MISMATCH"),
        ({"token": "wrong"}, "INVALID_OR_USED"),
        ({"now": NOW + timedelta(minutes=6)}, "EXPIRED"),
        ({"customer_notice_sent": False}, "NOTICE_REQUIRED"),
    ),
)
def test_grant_binding_expiry_and_notice_fail_closed(changes, error) -> None:
    service = bundle()
    grant, token = issue(service)
    supplied = changes.get("token", token)
    overrides = {key: value for key, value in changes.items() if key != "token"}
    with pytest.raises(ValueError, match=error):
        record(service, grant.grant_id, supplied, **overrides)


@pytest.mark.parametrize(
    "changes,error",
    (
        ({"mime_type": "text/html"}, "FILE_TYPE_OR_SIZE"),
        ({"size_bytes": 11 * 1024 * 1024}, "FILE_TYPE_OR_SIZE"),
        ({"exif_removed": False}, "SANITIZATION_REQUIRED"),
        ({"malware_clean": False}, "SANITIZATION_REQUIRED"),
        ({"fixed_guide_frame_used": False}, "GUIDANCE"),
        ({"realtime_prohibited_content_scan_passed": False}, "GUIDANCE"),
        ({"depicts_package_at_designated_location": False}, "PURPOSE_MISMATCH"),
        ({"gallery_upload": True}, "IN_APP_CAPTURE_REQUIRED"),
        ({"server_nonce_visible": False}, "IN_APP_CAPTURE_REQUIRED"),
        ({"burst_frame_sha256": ("a" * 64,)}, "BURST_FRAMES_REQUIRED"),
    ),
)
def test_file_safety_and_capture_purpose_are_enforced(changes, error) -> None:
    service = bundle()
    grant, token = issue(service)
    with pytest.raises(ValueError, match=error):
        record(service, grant.grant_id, token, inspection=inspection(**changes))


@pytest.mark.parametrize(
    "flag",
    (
        "contains_person_or_body",
        "contains_home_interior",
        "contains_other_order_data",
        "contains_personal_belongings",
        "contains_mail_or_shipping_label",
        "contains_vehicle_plate",
        "contains_building_access_information",
    ),
)
def test_prohibited_people_interior_and_identifiers_are_rejected(flag: str) -> None:
    service = bundle()
    grant, token = issue(service)
    with pytest.raises(ValueError, match="PROHIBITED_CAPTURE_CONTENT"):
        record(service, grant.grant_id, token, inspection=inspection(**{flag: True}))


def test_doorplate_is_only_accepted_after_masking_and_original_discard() -> None:
    service = bundle()
    grant, token = issue(service)
    with pytest.raises(ValueError, match="MASKED_WITHOUT_ORIGINAL"):
        record(
            service,
            grant.grant_id,
            token,
            inspection=inspection(contains_doorplate=True, privacy_mask_applied=True,
                                  original_discarded_after_masking=False),
        )

    service = bundle()
    grant, token = issue(service)
    receipt = record(
        service,
        grant.grant_id,
        token,
        inspection=inspection(contains_doorplate=True, privacy_mask_applied=True,
                              original_discarded_after_masking=True),
    )
    assert receipt.file_sha256 == "a" * 64


def test_duplicate_photo_reuse_across_grants_is_rejected() -> None:
    service = bundle()
    first, first_token = issue(service)
    record(service, first.grant_id, first_token)
    second, second_token = service.issue_grant(
        order_id="order-2", assignment_id="assignment-2", rider_id="rider-2", now=NOW
    )

    with pytest.raises(ValueError, match="DUPLICATE_OR_REUSED"):
        record(
            service,
            second.grant_id,
            second_token,
            order_id="order-2",
            assignment_id="assignment-2",
            rider_id="rider-2",
        )


@pytest.mark.parametrize(
    "method",
    (
        EvidenceMethod.ONE_TIME_DELIVERY_CODE,
        EvidenceMethod.PACKAGE_SEAL,
        EvidenceMethod.CUSTOMER_CONFIRMATION,
    ),
)
def test_safe_alternatives_never_create_penalty(method: EvidenceMethod) -> None:
    service = bundle()
    receipt = service.record_alternative(
        order_id="order-1",
        assignment_id="assignment-1",
        rider_id="rider-1",
        stage=EvidenceStage.DELIVERY_LOCATION,
        method=method,
        reason="camera unavailable or unsafe capture environment",
        now=NOW,
        customer_notice_sent=True,
    )

    assert receipt.file_sha256 is None
    assert receipt.penalty_allowed is False
    assert receipt.liability_effect == "CONTEXT_ONLY_HUMAN_REVIEW_REQUIRED"


def test_expiry_deletes_normal_original_but_legal_hold_preserves_and_audits() -> None:
    service = bundle()
    first, token = issue(service)
    normal = record(service, first.grant_id, token)
    held_grant, held_token = service.issue_grant(
        order_id="order-2", assignment_id="assignment-2", rider_id="rider-2", now=NOW
    )
    held = record(
        service,
        held_grant.grant_id,
        held_token,
        order_id="order-2",
        assignment_id="assignment-2",
        rider_id="rider-2",
        inspection=inspection(sha256="b" * 64),
    )
    service.place_legal_hold(held.evidence_id)
    service.audit_original_access(
        evidence_id=held.evidence_id,
        actor_id="human-reviewer",
        purpose="customer dispute review",
        now=NOW + timedelta(days=1),
    )

    purged = service.purge_expired_originals(now=NOW + timedelta(days=8))

    assert normal.evidence_id in purged
    assert held.evidence_id not in purged
    assert service.receipt(normal.evidence_id).original_deleted_at is not None
    assert service.receipt(held.evidence_id).original_deleted_at is None
    assert service.access_log()[0].purpose == "customer dispute review"


def customer_complaint(**changes) -> UploadInspection:
    values = {
        "sha256": "d" * 64,
        "burst_frame_sha256": ("d" * 64, "e" * 64),
        "frame_consistency_digest": "f" * 64,
        "capture_purpose": CUSTOMER_COMPLAINT_PURPOSE,
        "depicts_package_at_designated_location": False,
    }
    values.update(changes)
    return inspection(**values)


def test_new_damage_after_normal_delivery_routes_to_tamper_human_review() -> None:
    service = bundle()
    grant, token = issue(service)
    delivery = record(service, grant.grant_id, token)

    review = service.review_customer_complaint(
        delivery_evidence_id=delivery.evidence_id,
        customer_capture=customer_complaint(exterior_appears_normal=False),
        newly_visible_exterior_damage=True,
        newly_broken_seal=True,
        reported_internal_leak=False,
        investigation_signals=("photo-reuse-suspected", "linked-device"),
    )

    assert review.disposition is ComplaintDisposition.POST_DELIVERY_TAMPER_REVIEW
    assert review.human_review_required
    assert not review.automatic_refund_allowed
    assert not review.automatic_rejection_allowed
    assert review.rider_payment_continues
    assert not review.rider_clawback_before_fault_finding


def test_internal_leak_and_legitimate_claim_remain_open_for_human_refund_review() -> None:
    service = bundle()
    grant, token = issue(service)
    delivery = record(service, grant.grant_id, token)

    review = service.review_customer_complaint(
        delivery_evidence_id=delivery.evidence_id,
        customer_capture=customer_complaint(exterior_appears_normal=False),
        newly_visible_exterior_damage=False,
        newly_broken_seal=False,
        reported_internal_leak=True,
    )

    assert review.disposition is ComplaintDisposition.HUMAN_REFUND_REVIEW
    assert review.rider_payment_continues


def test_past_or_gallery_customer_complaint_photo_is_rejected() -> None:
    service = bundle()
    grant, token = issue(service)
    delivery = record(service, grant.grant_id, token)

    with pytest.raises(ValueError, match="IN_APP_CAPTURE_REQUIRED"):
        service.review_customer_complaint(
            delivery_evidence_id=delivery.evidence_id,
            customer_capture=customer_complaint(gallery_upload=True),
            newly_visible_exterior_damage=True,
            newly_broken_seal=True,
            reported_internal_leak=False,
        )
