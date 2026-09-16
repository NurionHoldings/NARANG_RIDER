"""Privacy-preserving customer delivery proof and exterior-report contracts."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from .persistence import (
    ConcurrencyConflict,
    IdempotencyConflict,
    RecordKind,
    Repository,
    SensitiveDataRejected,
    TenantScopeError,
    UnitOfWorkFactory,
    canonical_payload_digest,
)

NOTICE_TEXT = "개봉 전 외관 이상이 있으면 먼저 신고해 주세요."
REPORT_BUTTON_LABEL = "외관 이상 신고"


class CustomerCapability(StrEnum):
    VIEW_DELIVERY_PROOF = "customer:proof:view"
    REPORT_EXTERIOR = "customer:exterior:report"


class ReviewDisposition(StrEnum):
    STANDARD_HUMAN_REVIEW = "STANDARD_HUMAN_REVIEW"
    POST_DELIVERY_TAMPER_REVIEW = "POST_DELIVERY_TAMPER_REVIEW"


class SealStatus(StrEnum):
    INTACT = "intact"
    BROKEN = "broken"
    NOT_APPLICABLE = "not_applicable"


class CustomerProofErrorCode(StrEnum):
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_MEDIA = "INVALID_MEDIA"
    INVALID_REQUEST = "INVALID_REQUEST"
    NONCE_REUSED = "NONCE_REUSED"
    NOT_FOUND = "NOT_FOUND"
    OWNERSHIP_MISMATCH = "OWNERSHIP_MISMATCH"
    RETENTION_DENIED = "RETENTION_DENIED"
    STALE_VERSION = "STALE_VERSION"


class CustomerProofRejected(RuntimeError):
    def __init__(self, code: CustomerProofErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CustomerContext:
    actor_id: str
    customer_id: str
    branch_id: str
    capabilities: frozenset[CustomerCapability]


@dataclass(frozen=True)
class SanitizedMedia:
    object_ref: str
    object_hash: str
    masked: bool
    exif_stripped: bool
    malware_clean: bool
    unmasked_original_persisted: bool = False


class MediaSanitizer(Protocol):
    """Injected adapter; production owns masking, EXIF removal and scanning."""

    def sanitize_live_frames(
        self, *, order_id: str, grant_id: str, frame_hashes: tuple[str, ...]
    ) -> SanitizedMedia: ...


@dataclass(frozen=True)
class CaptureScope:
    handoff_location_only: bool
    package_exterior_only: bool
    seal_visible: bool
    leakage_damage_tilt_only: bool
    people_or_body_parts: bool = False
    indoor_space: bool = False
    household_items: bool = False
    mail_or_labels: bool = False
    vehicle_plate: bool = False
    entry_credentials: bool = False

    @property
    def permitted(self) -> bool:
        return (
            self.handoff_location_only
            and self.package_exterior_only
            and self.seal_visible
            and self.leakage_damage_tilt_only
            and not any(
                (
                    self.people_or_body_parts,
                    self.indoor_space,
                    self.household_items,
                    self.mail_or_labels,
                    self.vehicle_plate,
                    self.entry_credentials,
                )
            )
        )


@dataclass(frozen=True)
class CustomerCaptureGrant:
    grant_id: str
    order_id: str
    customer_id: str
    nonce: str
    expires_at: datetime
    min_frames: int = 2
    max_frames: int = 3
    live_camera_only: bool = True
    gallery_upload_allowed: bool = False


@dataclass(frozen=True)
class DeliveryCompleteNotice:
    notice_id: str
    order_id: str
    masked_photo_ref: str
    completed_at: datetime
    seal_status: SealStatus
    notice_text: str = NOTICE_TEXT
    button_label: str = REPORT_BUTTON_LABEL
    refund_rights_limited_by_acknowledgement: bool = False


@dataclass(frozen=True)
class CustomerReportReceipt:
    report_id: str
    disposition: ReviewDisposition
    review_priority: int
    automatic_refund_denial_allowed: bool = False
    automatic_customer_sanction_allowed: bool = False
    automatic_liability_allowed: bool = False
    automatic_rider_clawback_allowed: bool = False


@dataclass(frozen=True)
class RetentionPolicy:
    expires_at: datetime
    tombstone_after_expiry: bool = True


class CustomerProofService:
    def __init__(
        self,
        repository: Repository,
        units: UnitOfWorkFactory,
        media: MediaSanitizer,
    ) -> None:
        self._repository = repository
        self._units = units
        self._media = media

    def register_customer_order(
        self,
        context: CustomerContext,
        *,
        order_id: str,
        idempotency_key: str,
    ) -> None:
        """Bind a vault-safe customer identity to an order at the application edge."""

        self._require(context, CustomerCapability.VIEW_DELIVERY_PROOF)
        payload = {
            "branch_id": context.branch_id,
            "order_id": order_id,
            "customer_id": context.customer_id,
        }
        self._commit(
            context,
            f"customer-order:{idempotency_key}",
            payload,
            ((RecordKind.PARTNER_EVENT, f"customer-order:{order_id}", payload, 0),),
        )

    def notify_delivery_complete(
        self,
        context: CustomerContext,
        *,
        order_id: str,
        masked_photo: SanitizedMedia,
        completed_at: datetime,
        seal_status: SealStatus,
        retention: RetentionPolicy,
        idempotency_key: str,
    ) -> DeliveryCompleteNotice:
        self._require(context, CustomerCapability.VIEW_DELIVERY_PROOF)
        self._owned_order(context, order_id)
        self._validate_sanitized(masked_photo)
        if completed_at.tzinfo is None or retention.expires_at.tzinfo is None:
            self._reject(CustomerProofErrorCode.INVALID_REQUEST, "timezone-aware dates required")
        notice_id = f"delivery-notice:{order_id}"
        payload = {
            "branch_id": context.branch_id,
            "order_id": order_id,
            "customer_id": context.customer_id,
            "masked_photo_ref": masked_photo.object_ref,
            "masked_photo_hash": masked_photo.object_hash,
            "completed_at": completed_at.isoformat(),
            "seal_status": seal_status.value,
            "notice_text": NOTICE_TEXT,
            "button_label": REPORT_BUTTON_LABEL,
            "precise_location_included": False,
            "rider_pii_included": False,
            "refund_rights_limited_by_acknowledgement": False,
            "expires_at": retention.expires_at.isoformat(),
            "tombstone_after_expiry": retention.tombstone_after_expiry,
        }
        receipt = self._commit(
            context,
            f"delivery-notice:{idempotency_key}",
            payload,
            (
                (RecordKind.PARTNER_EVENT, notice_id, payload, 0),
                self._outbox(context, notice_id, "DELIVERY_COMPLETE_NOTICE", 1),
            ),
        )
        if receipt.replayed:
            stored = self._record(context, notice_id)
            payload = dict(stored.payload)
        return DeliveryCompleteNotice(
            notice_id,
            order_id,
            str(payload["masked_photo_ref"]),
            datetime.fromisoformat(str(payload["completed_at"])),
            SealStatus(str(payload["seal_status"])),
        )

    def issue_report_grant(
        self,
        context: CustomerContext,
        *,
        order_id: str,
        grant_id: str,
        expires_at: datetime,
        idempotency_key: str,
    ) -> CustomerCaptureGrant:
        self._require(context, CustomerCapability.REPORT_EXTERIOR)
        self._owned_order(context, order_id)
        if expires_at.tzinfo is None:
            self._reject(CustomerProofErrorCode.INVALID_REQUEST, "grant expiry needs timezone")
        nonce = secrets.token_urlsafe(24)
        payload = {
            "branch_id": context.branch_id,
            "order_id": order_id,
            "customer_id": context.customer_id,
            "nonce_digest": hashlib.sha256(nonce.encode()).hexdigest(),
            "expires_at": expires_at.isoformat(),
            "min_frames": 2,
            "max_frames": 3,
            "live_camera_only": True,
            "gallery_upload_allowed": False,
            "consumed": False,
        }
        self._commit(
            context,
            f"customer-report-grant:{idempotency_key}",
            {"grant_id": grant_id, **payload},
            ((RecordKind.PARTNER_EVENT, f"customer-report-grant:{grant_id}", payload, 0),),
        )
        return CustomerCaptureGrant(grant_id, order_id, context.customer_id, nonce, expires_at)

    def submit_exterior_report(
        self,
        context: CustomerContext,
        *,
        grant_id: str,
        nonce: str,
        frame_hashes: tuple[str, ...],
        scope: CaptureScope,
        new_damage_visible: bool,
        new_seal_break_visible: bool,
        repeat_signal: bool,
        linked_signal: bool,
        reused_photo_signal: bool,
        retention: RetentionPolicy,
        now: datetime,
        idempotency_key: str,
    ) -> CustomerReportReceipt:
        self._require(context, CustomerCapability.REPORT_EXTERIOR)
        grant = self._record(context, f"customer-report-grant:{grant_id}")
        self._owned_payload(context, grant.payload)
        if grant.payload.get("consumed"):
            self._reject(CustomerProofErrorCode.NONCE_REUSED, "capture grant already consumed")
        if now > datetime.fromisoformat(str(grant.payload["expires_at"])):
            self._reject(CustomerProofErrorCode.INVALID_REQUEST, "capture grant expired")
        if hashlib.sha256(nonce.encode()).hexdigest() != grant.payload.get("nonce_digest"):
            self._reject(CustomerProofErrorCode.INVALID_REQUEST, "nonce binding rejected")
        if not 2 <= len(frame_hashes) <= 3 or len(set(frame_hashes)) != len(frame_hashes):
            self._reject(
                CustomerProofErrorCode.INVALID_MEDIA, "two or three unique frames required"
            )
        if not scope.permitted:
            self._reject(CustomerProofErrorCode.INVALID_MEDIA, "capture scope is not permitted")
        order_id = str(grant.payload["order_id"])
        self._owned_order(context, order_id)
        media = self._media.sanitize_live_frames(
            order_id=order_id, grant_id=grant_id, frame_hashes=frame_hashes
        )
        self._validate_sanitized(media)
        notice = self._record(context, f"delivery-notice:{order_id}")
        if media.object_hash == notice.payload.get("masked_photo_hash"):
            reused_photo_signal = True
        tamper_signal = new_damage_visible or new_seal_break_visible
        disposition = (
            ReviewDisposition.POST_DELIVERY_TAMPER_REVIEW
            if tamper_signal
            else ReviewDisposition.STANDARD_HUMAN_REVIEW
        )
        review_priority = 1 + sum((repeat_signal, linked_signal, reused_photo_signal))
        report_id = f"customer-report:{grant_id}"
        report = {
            "branch_id": context.branch_id,
            "order_id": order_id,
            "customer_id": context.customer_id,
            "grant_id": grant_id,
            "sanitized_media_ref": media.object_ref,
            "sanitized_media_hash": media.object_hash,
            "normal_proof_ref": notice.payload["masked_photo_ref"],
            "disposition": disposition.value,
            "review_priority": review_priority,
            "signals_are_priority_only": True,
            "human_review_required": True,
            "automatic_refund_denial_allowed": False,
            "automatic_customer_sanction_allowed": False,
            "automatic_liability_allowed": False,
            "automatic_rider_clawback_allowed": False,
            "refund_rights_limited_by_notice_acknowledgement": False,
            "expires_at": retention.expires_at.isoformat(),
            "tombstone_after_expiry": retention.tombstone_after_expiry,
        }
        consumed = {**dict(grant.payload), "consumed": True}
        self._commit(
            context,
            f"customer-report:{idempotency_key}",
            {"grant_id": grant_id, "frames": list(frame_hashes), "report": report},
            (
                (
                    RecordKind.PARTNER_EVENT,
                    f"customer-report-grant:{grant_id}",
                    consumed,
                    grant.version,
                ),
                (RecordKind.PARTNER_EVENT, report_id, report, 0),
                self._outbox(context, report_id, disposition.value, 1),
            ),
        )
        return CustomerReportReceipt(report_id, disposition, review_priority)

    def place_legal_hold(
        self,
        context: CustomerContext,
        *,
        report_id: str,
        approver_ids: tuple[str, str],
        expected_version: int,
        idempotency_key: str,
    ) -> None:
        report = self._record(context, report_id)
        self._owned_payload(context, report.payload)
        if len(set(approver_ids)) != 2 or any(not value for value in approver_ids):
            self._reject(CustomerProofErrorCode.RETENTION_DENIED, "two distinct approvals required")
        payload = {
            **dict(report.payload),
            "legal_hold": True,
            "legal_hold_approval_hashes": [
                hashlib.sha256(value.encode()).hexdigest() for value in approver_ids
            ],
        }
        self._commit(
            context,
            f"customer-legal-hold:{idempotency_key}",
            {"report_id": report_id, "approvers": list(approver_ids)},
            (
                (RecordKind.PARTNER_EVENT, report_id, payload, expected_version),
                self._outbox(context, report_id, "LEGAL_HOLD_PLACED", expected_version + 1),
            ),
        )

    def _owned_order(self, context: CustomerContext, order_id: str):
        record = self._record(context, f"customer-order:{order_id}")
        self._owned_payload(context, record.payload)
        return record

    @staticmethod
    def _owned_payload(context: CustomerContext, payload) -> None:
        if (
            payload.get("customer_id") != context.customer_id
            or payload.get("branch_id") != context.branch_id
        ):
            CustomerProofService._reject(
                CustomerProofErrorCode.OWNERSHIP_MISMATCH, "customer scope mismatch"
            )

    def _record(self, context: CustomerContext, record_id: str):
        record = self._repository.get(RecordKind.PARTNER_EVENT, context.branch_id, record_id)
        if record is None:
            self._reject(CustomerProofErrorCode.NOT_FOUND, "record not found in branch")
        return record

    @staticmethod
    def _validate_sanitized(media: SanitizedMedia) -> None:
        if (
            not media.object_ref.startswith("evidence:")
            or len(media.object_hash) != 64
            or not media.masked
            or not media.exif_stripped
            or not media.malware_clean
            or media.unmasked_original_persisted
        ):
            CustomerProofService._reject(
                CustomerProofErrorCode.INVALID_MEDIA, "pre-storage privacy gate failed"
            )

    @staticmethod
    def _require(context: CustomerContext, capability: CustomerCapability) -> None:
        if capability not in context.capabilities:
            CustomerProofService._reject(
                CustomerProofErrorCode.AUTHORIZATION_DENIED, "capability missing"
            )

    @staticmethod
    def _outbox(context: CustomerContext, record_id: str, event: str, sequence: int):
        return (
            RecordKind.OUTBOX_MESSAGE,
            f"customer-proof:{record_id}:{event}:{sequence}",
            {
                "branch_id": context.branch_id,
                "record_id": record_id,
                "event": event,
                "sequence": sequence,
                "requires_ledger": False,
            },
            0,
        )

    def _commit(self, context: CustomerContext, key: str, digest, writes):
        unit = self._units.begin(
            branch_id=context.branch_id,
            idempotency_key=key,
            payload_digest=canonical_payload_digest(digest),
        )
        try:
            for kind, record_id, payload, version in writes:
                unit.put(kind, record_id, payload, expected_version=version)
            return unit.commit()
        except IdempotencyConflict as error:
            unit.rollback()
            raise CustomerProofRejected(
                CustomerProofErrorCode.IDEMPOTENCY_CONFLICT, "idempotency payload changed"
            ) from error
        except ConcurrencyConflict as error:
            unit.rollback()
            raise CustomerProofRejected(
                CustomerProofErrorCode.STALE_VERSION, "concurrent change rejected"
            ) from error
        except (SensitiveDataRejected, TenantScopeError, ValueError) as error:
            unit.rollback()
            raise CustomerProofRejected(
                CustomerProofErrorCode.INVALID_REQUEST, "command rejected"
            ) from error

    @staticmethod
    def _reject(code: CustomerProofErrorCode, message: str) -> None:
        raise CustomerProofRejected(code, message)


@dataclass(frozen=True)
class CustomerProofRouteContract:
    route_id: str
    method: str
    path_template: str
    request_dto: str | None
    response_dto: str
    capability: CustomerCapability
    idempotent: bool


CUSTOMER_PROOF_ROUTE_MANIFEST = (
    CustomerProofRouteContract(
        "delivery_notice",
        "GET",
        "/api/v1/customer/orders/{order_id}/delivery-proof",
        None,
        "DeliveryCompleteNoticeV1",
        CustomerCapability.VIEW_DELIVERY_PROOF,
        False,
    ),
    CustomerProofRouteContract(
        "report_grant",
        "POST",
        "/api/v1/customer/orders/{order_id}/exterior-report-grants",
        "LiveCaptureGrantRequestV1",
        "CustomerCaptureGrantV1",
        CustomerCapability.REPORT_EXTERIOR,
        True,
    ),
    CustomerProofRouteContract(
        "exterior_report",
        "POST",
        "/api/v1/customer/orders/{order_id}/exterior-reports",
        "ExteriorReportV1",
        "CustomerReportReceiptV1",
        CustomerCapability.REPORT_EXTERIOR,
        True,
    ),
)
