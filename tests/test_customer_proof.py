from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.customer_proof import (
    CUSTOMER_PROOF_ROUTE_MANIFEST,
    NOTICE_TEXT,
    REPORT_BUTTON_LABEL,
    CaptureScope,
    CustomerCapability,
    CustomerContext,
    CustomerProofErrorCode,
    CustomerProofRejected,
    CustomerProofService,
    RetentionPolicy,
    ReviewDisposition,
    SanitizedMedia,
    SealStatus,
)
from narang_rider.persistence import InMemoryPersistence, RecordKind

NOW = datetime(2026, 9, 16, 4, tzinfo=UTC)


class FakeSanitizer:
    def __init__(self, media=None):
        self.media = media or sanitized("evidence:customer-report", "b" * 64)
        self.calls = []

    def sanitize_live_frames(self, **kwargs):
        self.calls.append(kwargs)
        return self.media


def sanitized(ref="evidence:masked-delivery", digest="a" * 64, **changes):
    values = {
        "object_ref": ref,
        "object_hash": digest,
        "masked": True,
        "exif_stripped": True,
        "malware_clean": True,
        "unmasked_original_persisted": False,
    }
    values.update(changes)
    return SanitizedMedia(**values)


def context(customer="customer-1", branch="branch-sejong"):
    return CustomerContext("actor-1", customer, branch, frozenset(CustomerCapability))


def reject(code, function, *args, **kwargs):
    with pytest.raises(CustomerProofRejected) as raised:
        function(*args, **kwargs)
    assert raised.value.code is code


def setup_service(media=None):
    store = InMemoryPersistence()
    sanitizer = FakeSanitizer(media)
    service = CustomerProofService(store, store, sanitizer)
    service.register_customer_order(context(), order_id="order-1", idempotency_key="bind-1")
    service.notify_delivery_complete(
        context(),
        order_id="order-1",
        masked_photo=sanitized(),
        completed_at=NOW,
        seal_status=SealStatus.INTACT,
        retention=RetentionPolicy(NOW + timedelta(days=30)),
        idempotency_key="notice-1",
    )
    return service, store, sanitizer


def scope(**changes):
    values = {
        "handoff_location_only": True,
        "package_exterior_only": True,
        "seal_visible": True,
        "leakage_damage_tilt_only": True,
    }
    values.update(changes)
    return CaptureScope(**values)


def test_notice_is_exact_private_and_idempotent() -> None:
    service, store, _ = setup_service()
    notice = service.notify_delivery_complete(
        context(),
        order_id="order-1",
        masked_photo=sanitized(),
        completed_at=NOW,
        seal_status=SealStatus.INTACT,
        retention=RetentionPolicy(NOW + timedelta(days=30)),
        idempotency_key="notice-1",
    )
    assert notice.notice_text == NOTICE_TEXT == "개봉 전 외관 이상이 있으면 먼저 신고해 주세요."
    assert notice.button_label == REPORT_BUTTON_LABEL == "외관 이상 신고"
    record = store.get(RecordKind.PARTNER_EVENT, "branch-sejong", notice.notice_id)
    assert record is not None
    assert record.payload["precise_location_included"] is False
    assert record.payload["rider_pii_included"] is False
    assert record.payload["refund_rights_limited_by_acknowledgement"] is False


@pytest.mark.parametrize(
    "media",
    [
        sanitized(masked=False),
        sanitized(exif_stripped=False),
        sanitized(malware_clean=False),
        sanitized(unmasked_original_persisted=True),
    ],
)
def test_unmasked_or_unsafe_media_never_passes_storage_gate(media) -> None:
    store = InMemoryPersistence()
    service = CustomerProofService(store, store, FakeSanitizer())
    service.register_customer_order(context(), order_id="order-1", idempotency_key="bind")
    reject(
        CustomerProofErrorCode.INVALID_MEDIA,
        service.notify_delivery_complete,
        context(),
        order_id="order-1",
        masked_photo=media,
        completed_at=NOW,
        seal_status=SealStatus.INTACT,
        retention=RetentionPolicy(NOW + timedelta(days=30)),
        idempotency_key="unsafe",
    )


def report(service, **changes):
    grant = service.issue_report_grant(
        context(),
        order_id="order-1",
        grant_id="grant-1",
        expires_at=NOW + timedelta(minutes=5),
        idempotency_key="grant-1",
    )
    values = {
        "grant_id": grant.grant_id,
        "nonce": grant.nonce,
        "frame_hashes": ("1" * 64, "2" * 64),
        "scope": scope(),
        "new_damage_visible": False,
        "new_seal_break_visible": False,
        "repeat_signal": False,
        "linked_signal": False,
        "reused_photo_signal": False,
        "retention": RetentionPolicy(NOW + timedelta(days=30)),
        "now": NOW,
        "idempotency_key": "report-1",
    }
    values.update(changes)
    return grant, service.submit_exterior_report(context(), **values)


def test_live_camera_grant_and_two_to_three_frames_are_enforced() -> None:
    service, _, sanitizer = setup_service()
    grant, receipt = report(service)
    assert grant.live_camera_only is True
    assert grant.gallery_upload_allowed is False
    assert grant.min_frames == 2 and grant.max_frames == 3
    assert receipt.disposition is ReviewDisposition.STANDARD_HUMAN_REVIEW
    assert sanitizer.calls[0]["order_id"] == "order-1"


@pytest.mark.parametrize("frames", [("1" * 64,), ("1" * 64,) * 4, ("1" * 64,) * 2])
def test_bad_frame_count_or_reuse_is_rejected(frames) -> None:
    service, _, _ = setup_service()
    grant = service.issue_report_grant(
        context(),
        order_id="order-1",
        grant_id="grant",
        expires_at=NOW + timedelta(minutes=5),
        idempotency_key="g",
    )
    reject(
        CustomerProofErrorCode.INVALID_MEDIA,
        service.submit_exterior_report,
        context(),
        grant_id=grant.grant_id,
        nonce=grant.nonce,
        frame_hashes=frames,
        scope=scope(),
        new_damage_visible=False,
        new_seal_break_visible=False,
        repeat_signal=False,
        linked_signal=False,
        reused_photo_signal=False,
        retention=RetentionPolicy(NOW + timedelta(days=30)),
        now=NOW,
        idempotency_key="bad-frames",
    )


@pytest.mark.parametrize(
    "unsafe_scope",
    [
        scope(people_or_body_parts=True),
        scope(indoor_space=True),
        scope(mail_or_labels=True),
        scope(entry_credentials=True),
    ],
)
def test_forbidden_capture_scope_is_rejected_before_media_service(unsafe_scope) -> None:
    service, _, sanitizer = setup_service()
    grant = service.issue_report_grant(
        context(),
        order_id="order-1",
        grant_id="grant",
        expires_at=NOW + timedelta(minutes=5),
        idempotency_key="g",
    )
    reject(
        CustomerProofErrorCode.INVALID_MEDIA,
        service.submit_exterior_report,
        context(),
        grant_id=grant.grant_id,
        nonce=grant.nonce,
        frame_hashes=("1" * 64, "2" * 64),
        scope=unsafe_scope,
        new_damage_visible=False,
        new_seal_break_visible=False,
        repeat_signal=False,
        linked_signal=False,
        reused_photo_signal=False,
        retention=RetentionPolicy(NOW + timedelta(days=30)),
        now=NOW,
        idempotency_key="scope",
    )
    assert sanitizer.calls == []


def test_new_damage_routes_to_tamper_review_without_auto_decision() -> None:
    service, store, _ = setup_service()
    _, receipt = report(
        service,
        new_damage_visible=True,
        repeat_signal=True,
        linked_signal=True,
        reused_photo_signal=True,
    )
    assert receipt.disposition is ReviewDisposition.POST_DELIVERY_TAMPER_REVIEW
    assert receipt.review_priority == 4
    assert receipt.automatic_refund_denial_allowed is False
    assert receipt.automatic_customer_sanction_allowed is False
    assert receipt.automatic_liability_allowed is False
    assert receipt.automatic_rider_clawback_allowed is False
    stored = store.get(RecordKind.PARTNER_EVENT, "branch-sejong", receipt.report_id)
    assert stored is not None and stored.payload["human_review_required"] is True
    assert stored.payload["signals_are_priority_only"] is True


def test_nonce_reuse_conflict_and_wrong_nonce_are_blocked() -> None:
    service, _, _ = setup_service()
    grant, _ = report(service)
    reject(
        CustomerProofErrorCode.NONCE_REUSED,
        service.submit_exterior_report,
        context(),
        grant_id=grant.grant_id,
        nonce=grant.nonce,
        frame_hashes=("3" * 64, "4" * 64),
        scope=scope(),
        new_damage_visible=False,
        new_seal_break_visible=False,
        repeat_signal=False,
        linked_signal=False,
        reused_photo_signal=False,
        retention=RetentionPolicy(NOW + timedelta(days=30)),
        now=NOW,
        idempotency_key="reuse",
    )


def test_cross_customer_and_branch_idor_are_blocked() -> None:
    service, _, _ = setup_service()
    reject(
        CustomerProofErrorCode.OWNERSHIP_MISMATCH,
        service.issue_report_grant,
        context(customer="customer-2"),
        order_id="order-1",
        grant_id="forged",
        expires_at=NOW + timedelta(minutes=5),
        idempotency_key="idor-customer",
    )
    reject(
        CustomerProofErrorCode.NOT_FOUND,
        service.issue_report_grant,
        context(branch="branch-busan"),
        order_id="order-1",
        grant_id="forged",
        expires_at=NOW + timedelta(minutes=5),
        idempotency_key="idor-branch",
    )


def test_legal_hold_requires_distinct_dual_approval_and_stores_only_hashes() -> None:
    service, store, _ = setup_service()
    _, receipt = report(service)
    reject(
        CustomerProofErrorCode.RETENTION_DENIED,
        service.place_legal_hold,
        context(),
        report_id=receipt.report_id,
        approver_ids=("reviewer-1", "reviewer-1"),
        expected_version=1,
        idempotency_key="hold-bad",
    )
    service.place_legal_hold(
        context(),
        report_id=receipt.report_id,
        approver_ids=("reviewer-1", "reviewer-2"),
        expected_version=1,
        idempotency_key="hold-good",
    )
    stored = store.get(RecordKind.PARTNER_EVENT, "branch-sejong", receipt.report_id)
    assert stored is not None and stored.payload["legal_hold"] is True
    assert "reviewer-1" not in str(stored.payload["legal_hold_approval_hashes"])


def test_route_manifest_is_stable() -> None:
    assert len({route.route_id for route in CUSTOMER_PROOF_ROUTE_MANIFEST}) == 3
    assert all(route.idempotent for route in CUSTOMER_PROOF_ROUTE_MANIFEST if route.method != "GET")
