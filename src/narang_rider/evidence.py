from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import uuid4

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_ALLOWED_MIME = frozenset({"image/jpeg", "image/png", "image/webp"})
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
DELIVERY_EVIDENCE_PURPOSE = "DELIVERY_PACKAGE_AT_DESIGNATED_HANDOFF_LOCATION"
CUSTOMER_COMPLAINT_PURPOSE = "REALTIME_CUSTOMER_DAMAGE_COMPLAINT"


class EvidenceStage(StrEnum):
    MERCHANT_PACKED = "MERCHANT_PACKED"
    RIDER_PICKUP = "RIDER_PICKUP"
    DELIVERY_LOCATION = "DELIVERY_LOCATION"
    CUSTOMER_RECEIPT = "CUSTOMER_RECEIPT"


class EvidenceMethod(StrEnum):
    SANITIZED_PHOTO = "SANITIZED_PHOTO"
    ONE_TIME_DELIVERY_CODE = "ONE_TIME_DELIVERY_CODE"
    PACKAGE_SEAL = "PACKAGE_SEAL"
    CUSTOMER_CONFIRMATION = "CUSTOMER_CONFIRMATION"


@dataclass(frozen=True)
class CaptureGrant:
    grant_id: str
    order_id: str
    assignment_id: str
    rider_id: str
    token_digest: str
    expires_at: datetime
    used_at: datetime | None = None


@dataclass(frozen=True)
class UploadInspection:
    sha256: str
    mime_type: str
    size_bytes: int
    exif_removed: bool
    malware_clean: bool
    privacy_mask_applied: bool
    original_discarded_after_masking: bool
    fixed_guide_frame_used: bool
    realtime_prohibited_content_scan_passed: bool
    depicts_package_at_designated_location: bool
    captured_in_app_camera: bool
    gallery_upload: bool
    server_nonce_visible: bool
    burst_frame_sha256: tuple[str, ...]
    frame_consistency_digest: str
    capture_purpose: str
    exterior_appears_normal: bool
    seal_appears_intact: bool
    contains_person_or_body: bool = False
    contains_doorplate: bool = False
    contains_home_interior: bool = False
    contains_precise_address: bool = False
    contains_other_order_data: bool = False
    contains_personal_belongings: bool = False
    contains_mail_or_shipping_label: bool = False
    contains_vehicle_plate: bool = False
    contains_building_access_information: bool = False

    def validate(self) -> None:
        if _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("INVALID_EVIDENCE_DIGEST")
        if not 2 <= len(self.burst_frame_sha256) <= 3 or any(
            _SHA256.fullmatch(value) is None for value in self.burst_frame_sha256
        ):
            raise ValueError("TWO_OR_THREE_VALID_BURST_FRAMES_REQUIRED")
        if self.sha256 not in self.burst_frame_sha256:
            raise ValueError("SELECTED_FRAME_MUST_BELONG_TO_BURST")
        if _SHA256.fullmatch(self.frame_consistency_digest) is None:
            raise ValueError("FRAME_CONSISTENCY_DIGEST_REQUIRED")
        if self.mime_type not in _ALLOWED_MIME or not 0 < self.size_bytes <= _MAX_IMAGE_BYTES:
            raise ValueError("UNSAFE_EVIDENCE_FILE_TYPE_OR_SIZE")
        if not self.exif_removed or not self.malware_clean:
            raise ValueError("EVIDENCE_SANITIZATION_REQUIRED")
        prohibited = any(
            (
                self.contains_person_or_body,
                self.contains_home_interior,
                self.contains_other_order_data,
                self.contains_personal_belongings,
                self.contains_mail_or_shipping_label,
                self.contains_vehicle_plate,
                self.contains_building_access_information,
            )
        )
        if prohibited:
            raise ValueError("PROHIBITED_CAPTURE_CONTENT")
        location_identifier = self.contains_doorplate or self.contains_precise_address
        if location_identifier and not (
            self.privacy_mask_applied and self.original_discarded_after_masking
        ):
            raise ValueError("LOCATION_IDENTIFIER_MUST_BE_MASKED_WITHOUT_ORIGINAL")
        if not self.fixed_guide_frame_used or not self.realtime_prohibited_content_scan_passed:
            raise ValueError("CAPTURE_GUIDANCE_AND_REALTIME_SCAN_REQUIRED")
        if not self.captured_in_app_camera or self.gallery_upload or not self.server_nonce_visible:
            raise ValueError("SERVER_NONCE_IN_APP_CAPTURE_REQUIRED")
        if self.capture_purpose not in {
            DELIVERY_EVIDENCE_PURPOSE,
            CUSTOMER_COMPLAINT_PURPOSE,
        }:
            raise ValueError("DELIVERY_EVIDENCE_PURPOSE_MISMATCH")
        if (
            self.capture_purpose == DELIVERY_EVIDENCE_PURPOSE
            and not self.depicts_package_at_designated_location
        ):
            raise ValueError("DELIVERY_EVIDENCE_PURPOSE_MISMATCH")


@dataclass(frozen=True)
class EvidenceReceipt:
    evidence_id: str
    order_id: str
    assignment_id: str
    rider_id: str
    stage: EvidenceStage
    method: EvidenceMethod
    received_at: datetime
    retention_until: datetime
    file_sha256: str | None
    chain_refs: tuple[str, ...]
    customer_notice_sent: bool
    no_capture_reason: str | None = None
    legal_hold: bool = False
    original_deleted_at: datetime | None = None
    liability_effect: str = "CONTEXT_ONLY_HUMAN_REVIEW_REQUIRED"
    penalty_allowed: bool = False
    purpose: str = DELIVERY_EVIDENCE_PURPOSE
    reusable_for_ai_or_marketing: bool = False
    approximate_delivery_zone: str = ""
    seal_number: str = ""
    frame_consistency_digest: str = ""
    exterior_appeared_normal: bool = False
    seal_appeared_intact: bool = False
    server_signature: str = ""


class ComplaintDisposition(StrEnum):
    POST_DELIVERY_TAMPER_REVIEW = "POST_DELIVERY_TAMPER_REVIEW"
    HUMAN_REFUND_REVIEW = "HUMAN_REFUND_REVIEW"


@dataclass(frozen=True)
class ComplaintReview:
    disposition: ComplaintDisposition
    human_review_required: bool
    automatic_refund_allowed: bool
    automatic_rejection_allowed: bool
    rider_payment_continues: bool
    rider_clawback_before_fault_finding: bool
    investigation_priority_signals: tuple[str, ...]


@dataclass(frozen=True)
class CustomerEvidenceNotice:
    evidence_id: str
    received_at: datetime
    exterior_appeared_normal: bool
    seal_appeared_intact: bool
    dispute_route: str = "IN_APP_REALTIME_COMPLAINT"


@dataclass(frozen=True)
class EvidenceAccess:
    evidence_id: str
    actor_id: str
    purpose: str
    accessed_at: datetime


class DeliveryEvidenceBundle:
    """Minimal in-memory policy boundary; blob storage remains an external adapter."""

    def __init__(
        self, *, grant_ttl: timedelta, normal_retention: timedelta, signing_key: bytes
    ) -> None:
        if grant_ttl <= timedelta(0) or normal_retention <= timedelta(0):
            raise ValueError("POSITIVE_EVIDENCE_RETENTION_REQUIRED")
        if len(signing_key) < 32:
            raise ValueError("STRONG_EVIDENCE_SIGNING_KEY_REQUIRED")
        self.grant_ttl = grant_ttl
        self.normal_retention = normal_retention
        self._signing_key = signing_key
        self._grants: dict[str, CaptureGrant] = {}
        self._receipts: dict[str, EvidenceReceipt] = {}
        self._digests: set[str] = set()
        self._access_log: list[EvidenceAccess] = []

    def issue_grant(
        self,
        *,
        order_id: str,
        assignment_id: str,
        rider_id: str,
        now: datetime,
    ) -> tuple[CaptureGrant, str]:
        _require_aware(now)
        if not all(value.strip() for value in (order_id, assignment_id, rider_id)):
            raise ValueError("EVIDENCE_BINDING_REQUIRED")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        grant = CaptureGrant(
            grant_id=str(uuid4()),
            order_id=order_id,
            assignment_id=assignment_id,
            rider_id=rider_id,
            token_digest=digest,
            expires_at=now + self.grant_ttl,
        )
        self._grants[grant.grant_id] = grant
        return grant, token

    def record_photo(
        self,
        *,
        grant_id: str,
        token: str,
        order_id: str,
        assignment_id: str,
        rider_id: str,
        stage: EvidenceStage,
        inspection: UploadInspection,
        now: datetime,
        chain_refs: tuple[str, ...] = (),
        customer_notice_sent: bool,
        approximate_delivery_zone: str,
        seal_number: str,
    ) -> EvidenceReceipt:
        _require_aware(now)
        grant = self._grants.get(grant_id)
        if grant is None or grant.used_at is not None:
            raise ValueError("INVALID_OR_USED_CAPTURE_GRANT")
        if now > grant.expires_at:
            raise ValueError("EXPIRED_CAPTURE_GRANT")
        supplied_digest = hashlib.sha256(token.encode()).hexdigest()
        if not secrets.compare_digest(supplied_digest, grant.token_digest):
            raise ValueError("INVALID_OR_USED_CAPTURE_GRANT")
        if (order_id, assignment_id, rider_id) != (
            grant.order_id,
            grant.assignment_id,
            grant.rider_id,
        ):
            raise ValueError("CAPTURE_GRANT_BINDING_MISMATCH")
        if not customer_notice_sent:
            raise ValueError("CUSTOMER_EVIDENCE_NOTICE_REQUIRED")
        inspection.validate()
        if inspection.capture_purpose != DELIVERY_EVIDENCE_PURPOSE:
            raise ValueError("DELIVERY_EVIDENCE_PURPOSE_MISMATCH")
        if not approximate_delivery_zone.strip() or not seal_number.strip():
            raise ValueError("ZONE_AND_SEAL_REQUIRED")
        if inspection.sha256 in self._digests:
            raise ValueError("DUPLICATE_OR_REUSED_EVIDENCE")
        evidence_id = str(uuid4())
        signature = self._sign(
            evidence_id,
            order_id,
            assignment_id,
            rider_id,
            now.isoformat(),
            approximate_delivery_zone,
            seal_number,
            inspection.sha256,
            inspection.frame_consistency_digest,
        )
        receipt = EvidenceReceipt(
            evidence_id=evidence_id,
            order_id=order_id,
            assignment_id=assignment_id,
            rider_id=rider_id,
            stage=stage,
            method=EvidenceMethod.SANITIZED_PHOTO,
            received_at=now,
            retention_until=now + self.normal_retention,
            file_sha256=inspection.sha256,
            chain_refs=chain_refs,
            customer_notice_sent=True,
            approximate_delivery_zone=approximate_delivery_zone,
            seal_number=seal_number,
            frame_consistency_digest=inspection.frame_consistency_digest,
            exterior_appeared_normal=inspection.exterior_appears_normal,
            seal_appeared_intact=inspection.seal_appears_intact,
            server_signature=signature,
        )
        self._grants[grant_id] = replace(grant, used_at=now)
        self._digests.add(inspection.sha256)
        self._receipts[receipt.evidence_id] = receipt
        return receipt

    def record_alternative(
        self,
        *,
        order_id: str,
        assignment_id: str,
        rider_id: str,
        stage: EvidenceStage,
        method: EvidenceMethod,
        reason: str,
        now: datetime,
        chain_refs: tuple[str, ...] = (),
        customer_notice_sent: bool,
    ) -> EvidenceReceipt:
        _require_aware(now)
        if method is EvidenceMethod.SANITIZED_PHOTO or not reason.strip():
            raise ValueError("VALID_ALTERNATIVE_EVIDENCE_REQUIRED")
        if not customer_notice_sent:
            raise ValueError("CUSTOMER_EVIDENCE_NOTICE_REQUIRED")
        receipt = EvidenceReceipt(
            evidence_id=str(uuid4()),
            order_id=order_id,
            assignment_id=assignment_id,
            rider_id=rider_id,
            stage=stage,
            method=method,
            received_at=now,
            retention_until=now + self.normal_retention,
            file_sha256=None,
            chain_refs=chain_refs,
            customer_notice_sent=True,
            no_capture_reason=reason,
        )
        self._receipts[receipt.evidence_id] = receipt
        return receipt

    def place_legal_hold(self, evidence_id: str) -> EvidenceReceipt:
        receipt = self._receipts[evidence_id]
        held = replace(receipt, legal_hold=True)
        self._receipts[evidence_id] = held
        return held

    def purge_expired_originals(self, *, now: datetime) -> tuple[str, ...]:
        _require_aware(now)
        purged = []
        for evidence_id, receipt in tuple(self._receipts.items()):
            if (
                receipt.file_sha256 is not None
                and receipt.original_deleted_at is None
                and not receipt.legal_hold
                and now >= receipt.retention_until
            ):
                self._receipts[evidence_id] = replace(receipt, original_deleted_at=now)
                purged.append(evidence_id)
        return tuple(purged)

    def audit_original_access(
        self, *, evidence_id: str, actor_id: str, purpose: str, now: datetime
    ) -> None:
        _require_aware(now)
        receipt = self._receipts[evidence_id]
        if receipt.original_deleted_at is not None:
            raise ValueError("EVIDENCE_ORIGINAL_DELETED")
        if not actor_id.strip() or not purpose.strip():
            raise ValueError("EVIDENCE_ACCESS_JUSTIFICATION_REQUIRED")
        self._access_log.append(EvidenceAccess(evidence_id, actor_id, purpose, now))

    def receipt(self, evidence_id: str) -> EvidenceReceipt:
        return self._receipts[evidence_id]

    def customer_notice(self, evidence_id: str) -> CustomerEvidenceNotice:
        receipt = self._receipts[evidence_id]
        return CustomerEvidenceNotice(
            evidence_id=receipt.evidence_id,
            received_at=receipt.received_at,
            exterior_appeared_normal=receipt.exterior_appeared_normal,
            seal_appeared_intact=receipt.seal_appeared_intact,
        )

    def access_log(self) -> tuple[EvidenceAccess, ...]:
        return tuple(self._access_log)

    def review_customer_complaint(
        self,
        *,
        delivery_evidence_id: str,
        customer_capture: UploadInspection,
        newly_visible_exterior_damage: bool,
        newly_broken_seal: bool,
        reported_internal_leak: bool,
        investigation_signals: tuple[str, ...] = (),
    ) -> ComplaintReview:
        delivery = self._receipts[delivery_evidence_id]
        customer_capture.validate()
        if customer_capture.capture_purpose != CUSTOMER_COMPLAINT_PURPOSE:
            raise ValueError("CUSTOMER_COMPLAINT_CAPTURE_REQUIRED")
        suspected_tamper = (
            delivery.exterior_appeared_normal
            and delivery.seal_appeared_intact
            and (newly_visible_exterior_damage or newly_broken_seal)
            and not reported_internal_leak
        )
        return ComplaintReview(
            disposition=(
                ComplaintDisposition.POST_DELIVERY_TAMPER_REVIEW
                if suspected_tamper
                else ComplaintDisposition.HUMAN_REFUND_REVIEW
            ),
            human_review_required=True,
            automatic_refund_allowed=False,
            automatic_rejection_allowed=False,
            rider_payment_continues=True,
            rider_clawback_before_fault_finding=False,
            investigation_priority_signals=tuple(sorted(set(investigation_signals))),
        )

    def verify_receipt_signature(self, receipt: EvidenceReceipt) -> bool:
        expected = self._sign(
            receipt.evidence_id,
            receipt.order_id,
            receipt.assignment_id,
            receipt.rider_id,
            receipt.received_at.isoformat(),
            receipt.approximate_delivery_zone,
            receipt.seal_number,
            receipt.file_sha256 or "",
            receipt.frame_consistency_digest,
        )
        return hmac.compare_digest(expected, receipt.server_signature)

    def _sign(self, *values: str) -> str:
        payload = "\x1f".join(values).encode()
        return hmac.new(self._signing_key, payload, hashlib.sha256).hexdigest()


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("TIMEZONE_AWARE_TIMESTAMP_REQUIRED")
