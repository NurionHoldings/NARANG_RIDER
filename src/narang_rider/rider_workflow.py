"""Rider-facing workflow contracts with fair-offer and safety boundaries."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .merchant_operations import ActorSource
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


class RiderCapability(StrEnum):
    MANAGE_AVAILABILITY = "rider:availability:manage"
    VIEW_OFFERS = "rider:offer:view"
    RESPOND_OFFER = "rider:offer:respond"
    EXECUTE_DELIVERY = "rider:delivery:execute"
    CAPTURE_PROOF = "rider:proof:capture"
    VIEW_EARNINGS = "rider:earnings:view"


class RiderAvailability(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    BREAK = "break"
    SAFETY_STOP = "safety_stop"


class RiderOfferStatus(StrEnum):
    OPEN = "OPEN"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    TIMED_OUT = "TIMED_OUT"


class DeliveryStatus(StrEnum):
    ASSIGNED = "ASSIGNED"
    ARRIVED = "ARRIVED"
    PICKED_UP = "PICKED_UP"
    DELIVERED = "DELIVERED"


class RiderErrorCode(StrEnum):
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    FORBIDDEN_AI_AUTHORITY = "FORBIDDEN_AI_AUTHORITY"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    OWNERSHIP_MISMATCH = "OWNERSHIP_MISMATCH"
    STALE_LEASE = "STALE_LEASE"
    STALE_VERSION = "STALE_VERSION"
    STATE_TRANSITION_DENIED = "STATE_TRANSITION_DENIED"


class RiderCommandRejected(RuntimeError):
    def __init__(self, code: RiderErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RiderContext:
    actor_id: str
    rider_id: str
    branch_id: str
    capabilities: frozenset[RiderCapability]
    source: ActorSource = ActorSource.HUMAN


@dataclass(frozen=True)
class FairOfferView:
    offer_id: str
    order_id: str
    rider_id: str
    branch_id: str
    rider_pay_won: int
    estimated_cost_won: int
    estimated_net_won: int
    queue_position: int
    reason_codes: tuple[str, ...]
    receipt_id: str
    lease_until: datetime
    coarse_pickup_zone: str
    coarse_dropoff_zone: str
    route_ref: str
    version: int = 1

    def __post_init__(self) -> None:
        if min(self.rider_pay_won, self.estimated_cost_won, self.estimated_net_won) < 0:
            raise ValueError("offer money cannot be negative")
        if self.estimated_net_won != self.rider_pay_won - self.estimated_cost_won:
            raise ValueError("net estimate mismatch")
        if self.queue_position < 1 or not self.reason_codes:
            raise ValueError("explainable FIFO position is required")
        if self.lease_until.tzinfo is None or not self.route_ref.startswith("vault:"):
            raise ValueError("lease and vault route reference are required")
        for zone in (self.coarse_pickup_zone, self.coarse_dropoff_zone):
            if not zone or any(char in zone for char in (".", ",", "/")):
                raise ValueError("only coarse zone labels are allowed")


@dataclass(frozen=True)
class ProofCaptureGrant:
    grant_id: str
    order_id: str
    assignment_id: str
    rider_id: str
    nonce: str
    expires_at: datetime


@dataclass(frozen=True)
class RiderReceipt:
    record_id: str
    status: str
    version: int
    replayed: bool


class RiderWorkflowService:
    """Application boundary for the future rider app; no UI or GPS collection."""

    def __init__(self, repository: Repository, units: UnitOfWorkFactory) -> None:
        self._repository = repository
        self._units = units

    def set_availability(
        self,
        context: RiderContext,
        *,
        status: RiderAvailability,
        expected_version: int,
        idempotency_key: str,
        safety_reason_code: str | None = None,
    ) -> RiderReceipt:
        self._require(context, RiderCapability.MANAGE_AVAILABILITY)
        if status is RiderAvailability.SAFETY_STOP and not safety_reason_code:
            self._reject(RiderErrorCode.INVALID_REQUEST, "safety reason code required")
        record_id = f"rider-availability:{context.rider_id}"
        payload = {
            "branch_id": context.branch_id,
            "rider_id": context.rider_id,
            "status": status.value,
            "safety_reason_code": safety_reason_code,
            "penalty_allowed": False,
            "decline_history_used": False,
        }
        receipt = self._commit(
            context,
            f"rider-availability:{idempotency_key}",
            {"record_id": record_id, "expected_version": expected_version, **payload},
            ((RecordKind.PARTNER_EVENT, record_id, payload, expected_version),),
        )
        return RiderReceipt(record_id, status.value, expected_version + 1, receipt.replayed)

    def publish_offer(
        self, context: RiderContext, offer: FairOfferView, idempotency_key: str
    ) -> RiderReceipt:
        """Persist an already policy-ranked offer; this method cannot use declines."""

        self._require(context, RiderCapability.VIEW_OFFERS)
        self._owned(context, offer.rider_id, offer.branch_id)
        payload = {
            "branch_id": offer.branch_id,
            "rider_id": offer.rider_id,
            "order_id": offer.order_id,
            "status": RiderOfferStatus.OPEN.value,
            "rider_pay_won": offer.rider_pay_won,
            "estimated_cost_won": offer.estimated_cost_won,
            "estimated_net_won": offer.estimated_net_won,
            "queue_position": offer.queue_position,
            "reason_codes": list(offer.reason_codes),
            "receipt_id": offer.receipt_id,
            "lease_until": offer.lease_until.isoformat(),
            "coarse_pickup_zone": offer.coarse_pickup_zone,
            "coarse_dropoff_zone": offer.coarse_dropoff_zone,
            "route_ref": offer.route_ref,
            "decline_history_used": False,
            "decline_penalty_allowed": False,
        }
        receipt = self._commit(
            context,
            f"rider-offer-publish:{idempotency_key}",
            {"offer_id": offer.offer_id, **payload},
            ((RecordKind.PARTNER_EVENT, f"rider-offer:{offer.offer_id}", payload, 0),),
        )
        return RiderReceipt(offer.offer_id, RiderOfferStatus.OPEN, 1, receipt.replayed)

    def respond_offer(
        self,
        context: RiderContext,
        *,
        offer_id: str,
        accept: bool,
        now: datetime,
        expected_version: int,
        idempotency_key: str,
    ) -> RiderReceipt:
        self._human_only(context, "offer response")
        self._require(context, RiderCapability.RESPOND_OFFER)
        offer = self._record(context, RecordKind.PARTNER_EVENT, f"rider-offer:{offer_id}")
        self._owned(context, str(offer.payload.get("rider_id")), offer.branch_id)
        if offer.version != expected_version:
            self._reject(RiderErrorCode.STALE_VERSION, "offer version changed")
        if offer.payload.get("status") != RiderOfferStatus.OPEN.value:
            self._reject(RiderErrorCode.STATE_TRANSITION_DENIED, "offer is not open")
        lease_until = datetime.fromisoformat(str(offer.payload["lease_until"]))
        timed_out = now > lease_until
        status = (
            RiderOfferStatus.TIMED_OUT
            if timed_out
            else RiderOfferStatus.ACCEPTED
            if accept
            else RiderOfferStatus.DECLINED
        )
        if accept and timed_out:
            self._reject(RiderErrorCode.STALE_LEASE, "offer lease expired")
        offer_payload = {
            **dict(offer.payload),
            "status": status.value,
            "decline_penalty_allowed": False,
            "retaliation_allowed": False,
        }
        writes: list[tuple[RecordKind, str, dict[str, object], int]] = [
            (RecordKind.PARTNER_EVENT, f"rider-offer:{offer_id}", offer_payload, expected_version)
        ]
        record_id = offer_id
        if status is RiderOfferStatus.ACCEPTED:
            order_id = str(offer.payload["order_id"])
            assignment_id = self._assignment_id(order_id)
            assignment = {
                "branch_id": context.branch_id,
                "assignment_id": assignment_id,
                "order_id": order_id,
                "offer_id": offer_id,
                "rider_id": context.rider_id,
                "status": DeliveryStatus.ASSIGNED.value,
                "rider_pay_won": offer.payload["rider_pay_won"],
                "estimated_cost_won": offer.payload["estimated_cost_won"],
                "estimated_net_won": offer.payload["estimated_net_won"],
                "route_ref": offer.payload["route_ref"],
                "coarse_pickup_zone": offer.payload["coarse_pickup_zone"],
                "coarse_dropoff_zone": offer.payload["coarse_dropoff_zone"],
            }
            # Deterministic assignment key is the cross-rider concurrency lock.
            writes.append((RecordKind.PARTNER_EVENT, assignment_id, assignment, 0))
            record_id = assignment_id
        writes.append(
            self._outbox(context, record_id, f"OFFER_{status.value}", expected_version + 1)
        )
        receipt = self._commit(
            context,
            f"rider-offer-response:{idempotency_key}",
            {
                "offer_id": offer_id,
                "accept": accept,
                "now": now.isoformat(),
                "version": expected_version,
            },
            tuple(writes),
        )
        return RiderReceipt(record_id, status.value, expected_version + 1, receipt.replayed)

    def progress(
        self,
        context: RiderContext,
        *,
        assignment_id: str,
        target: DeliveryStatus,
        expected_version: int,
        idempotency_key: str,
    ) -> RiderReceipt:
        self._human_only(context, "delivery progress")
        self._require(context, RiderCapability.EXECUTE_DELIVERY)
        assignment = self._assignment(context, assignment_id)
        current = DeliveryStatus(str(assignment.payload["status"]))
        allowed = {
            DeliveryStatus.ASSIGNED: DeliveryStatus.ARRIVED,
            DeliveryStatus.ARRIVED: DeliveryStatus.PICKED_UP,
        }
        if allowed.get(current) is not target:
            self._reject(RiderErrorCode.STATE_TRANSITION_DENIED, "delivery status cannot skip")
        if assignment.version != expected_version:
            self._reject(RiderErrorCode.STALE_VERSION, "assignment version changed")
        payload = {**dict(assignment.payload), "status": target.value}
        if target is DeliveryStatus.PICKED_UP:
            order = self._record(context, RecordKind.ORDER, str(assignment.payload["order_id"]))
            payload.update(
                {
                    "double_packaging": order.payload.get("double_packaging", "not_applicable"),
                    "seal_number": order.payload.get("seal_number"),
                    "packaging_verified_by_rider": True,
                    "merchant_packaging_incomplete_is_rider_fault": False,
                }
            )
        receipt = self._commit(
            context,
            f"rider-progress:{idempotency_key}",
            {"assignment_id": assignment_id, "target": target.value, "version": expected_version},
            (
                (RecordKind.PARTNER_EVENT, assignment_id, payload, expected_version),
                self._outbox(context, assignment_id, target.value, expected_version + 1),
            ),
        )
        return RiderReceipt(assignment_id, target.value, expected_version + 1, receipt.replayed)

    def issue_proof_grant(
        self,
        context: RiderContext,
        *,
        assignment_id: str,
        grant_id: str,
        expires_at: datetime,
        idempotency_key: str,
    ) -> ProofCaptureGrant:
        self._human_only(context, "proof capture")
        self._require(context, RiderCapability.CAPTURE_PROOF)
        assignment = self._assignment(context, assignment_id)
        if assignment.payload.get("status") != DeliveryStatus.PICKED_UP.value:
            self._reject(RiderErrorCode.STATE_TRANSITION_DENIED, "pickup required")
        nonce = secrets.token_urlsafe(24)
        nonce_digest = hashlib.sha256(nonce.encode()).hexdigest()
        payload = {
            "branch_id": context.branch_id,
            "assignment_id": assignment_id,
            "order_id": assignment.payload["order_id"],
            "rider_id": context.rider_id,
            "nonce_digest": nonce_digest,
            "expires_at": expires_at.isoformat(),
            "consumed": False,
            "gallery_upload_allowed": False,
        }
        self._commit(
            context,
            f"rider-proof-grant:{idempotency_key}",
            {"grant_id": grant_id, **payload},
            ((RecordKind.PARTNER_EVENT, f"proof-grant:{grant_id}", payload, 0),),
        )
        return ProofCaptureGrant(
            grant_id,
            str(assignment.payload["order_id"]),
            assignment_id,
            context.rider_id,
            nonce,
            expires_at,
        )

    def complete_delivery(
        self,
        context: RiderContext,
        *,
        assignment_id: str,
        grant_id: str,
        nonce: str,
        evidence_receipt_ref: str,
        now: datetime,
        expected_assignment_version: int,
        idempotency_key: str,
    ) -> RiderReceipt:
        self._human_only(context, "delivery completion")
        self._require(context, RiderCapability.EXECUTE_DELIVERY, RiderCapability.CAPTURE_PROOF)
        assignment = self._assignment(context, assignment_id)
        if assignment.version != expected_assignment_version:
            self._reject(RiderErrorCode.STALE_VERSION, "assignment version changed")
        if assignment.payload.get("status") != DeliveryStatus.PICKED_UP.value:
            self._reject(RiderErrorCode.STATE_TRANSITION_DENIED, "pickup required")
        grant = self._record(context, RecordKind.PARTNER_EVENT, f"proof-grant:{grant_id}")
        self._owned(context, str(grant.payload.get("rider_id")), grant.branch_id)
        if grant.payload.get("assignment_id") != assignment_id or grant.payload.get("consumed"):
            self._reject(RiderErrorCode.INVALID_REQUEST, "grant binding or reuse rejected")
        if now > datetime.fromisoformat(str(grant.payload["expires_at"])):
            self._reject(RiderErrorCode.STALE_LEASE, "proof grant expired")
        if hashlib.sha256(nonce.encode()).hexdigest() != grant.payload.get("nonce_digest"):
            self._reject(RiderErrorCode.INVALID_REQUEST, "proof nonce rejected")
        if not evidence_receipt_ref.startswith("evidence:"):
            self._reject(RiderErrorCode.INVALID_REQUEST, "evidence receipt handoff required")
        assignment_payload = {
            **dict(assignment.payload),
            "status": DeliveryStatus.DELIVERED.value,
            "evidence_receipt_ref": evidence_receipt_ref,
        }
        grant_payload = {**dict(grant.payload), "consumed": True}
        earning_id = f"earning:{assignment_id}"
        earning = {
            "branch_id": context.branch_id,
            "assignment_id": assignment_id,
            "order_id": assignment.payload["order_id"],
            "rider_id": context.rider_id,
            "gross_won": assignment.payload["rider_pay_won"],
            "estimated_cost_won": assignment.payload["estimated_cost_won"],
            "estimated_net_won": assignment.payload["estimated_net_won"],
            "append_only": True,
            "evidence_receipt_ref": evidence_receipt_ref,
        }
        receipt = self._commit(
            context,
            f"rider-deliver:{idempotency_key}",
            {
                "assignment_id": assignment_id,
                "grant_id": grant_id,
                "evidence": evidence_receipt_ref,
            },
            (
                (
                    RecordKind.PARTNER_EVENT,
                    assignment_id,
                    assignment_payload,
                    expected_assignment_version,
                ),
                (RecordKind.PARTNER_EVENT, f"proof-grant:{grant_id}", grant_payload, grant.version),
                (RecordKind.LEDGER_TRANSACTION, earning_id, earning, 0),
                (
                    RecordKind.OUTBOX_MESSAGE,
                    f"rider-earning:{assignment_id}",
                    {
                        "branch_id": context.branch_id,
                        "assignment_id": assignment_id,
                        "event": "DELIVERED_EARNING_RECORDED",
                        "requires_ledger": True,
                    },
                    0,
                ),
            ),
        )
        return RiderReceipt(
            assignment_id,
            DeliveryStatus.DELIVERED,
            expected_assignment_version + 1,
            receipt.replayed,
        )

    def earnings_preview(self, context: RiderContext, assignment_id: str) -> dict[str, int]:
        self._require(context, RiderCapability.VIEW_EARNINGS)
        assignment = self._assignment(context, assignment_id)
        return {
            "gross_won": int(assignment.payload["rider_pay_won"]),
            "estimated_cost_won": int(assignment.payload["estimated_cost_won"]),
            "estimated_net_won": int(assignment.payload["estimated_net_won"]),
        }

    def eta_advisory(
        self, context: RiderContext, assignment_id: str, eta_minutes: int
    ) -> dict[str, Any]:
        self._assignment(context, assignment_id)
        if not 0 <= eta_minutes <= 240:
            self._reject(RiderErrorCode.INVALID_REQUEST, "invalid ETA")
        return {
            "assignment_id": assignment_id,
            "eta_minutes_advisory": eta_minutes,
            "may_exclude": False,
            "may_penalize": False,
            "may_suspend": False,
            "may_alter_pay": False,
            "may_alter_insurance": False,
        }

    def _assignment(self, context: RiderContext, assignment_id: str):
        record = self._record(context, RecordKind.PARTNER_EVENT, assignment_id)
        self._owned(context, str(record.payload.get("rider_id")), record.branch_id)
        return record

    def _record(self, context: RiderContext, kind: RecordKind, record_id: str):
        record = self._repository.get(kind, context.branch_id, record_id)
        if record is None:
            self._reject(RiderErrorCode.NOT_FOUND, "record not found in branch")
        return record

    @staticmethod
    def _owned(context: RiderContext, rider_id: str, branch_id: str) -> None:
        if rider_id != context.rider_id or branch_id != context.branch_id:
            RiderWorkflowService._reject(RiderErrorCode.OWNERSHIP_MISMATCH, "rider scope mismatch")

    @staticmethod
    def _require(context: RiderContext, *capabilities: RiderCapability) -> None:
        if not set(capabilities).issubset(context.capabilities):
            RiderWorkflowService._reject(RiderErrorCode.AUTHORIZATION_DENIED, "capability missing")

    @staticmethod
    def _human_only(context: RiderContext, action: str) -> None:
        if context.source is ActorSource.ARKAON:
            RiderWorkflowService._reject(
                RiderErrorCode.FORBIDDEN_AI_AUTHORITY, f"ARKAON cannot execute {action}"
            )

    @staticmethod
    def _assignment_id(order_id: str) -> str:
        return f"rider-assignment:{hashlib.sha256(order_id.encode()).hexdigest()[:24]}"

    @staticmethod
    def _outbox(context: RiderContext, record_id: str, event: str, sequence: int):
        return (
            RecordKind.OUTBOX_MESSAGE,
            f"rider-workflow:{record_id}:{event}:{sequence}",
            {
                "branch_id": context.branch_id,
                "record_id": record_id,
                "event": event,
                "sequence": sequence,
                "requires_ledger": False,
            },
            0,
        )

    def _commit(self, context: RiderContext, key: str, digest: dict[str, object], writes):
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
            raise RiderCommandRejected(
                RiderErrorCode.IDEMPOTENCY_CONFLICT, "idempotency payload changed"
            ) from error
        except ConcurrencyConflict as error:
            unit.rollback()
            raise RiderCommandRejected(
                RiderErrorCode.STALE_VERSION, "concurrent change rejected"
            ) from error
        except (SensitiveDataRejected, TenantScopeError, ValueError) as error:
            unit.rollback()
            raise RiderCommandRejected(
                RiderErrorCode.INVALID_REQUEST, "command rejected"
            ) from error

    @staticmethod
    def _reject(code: RiderErrorCode, message: str) -> None:
        raise RiderCommandRejected(code, message)


@dataclass(frozen=True)
class RiderRouteContract:
    route_id: str
    method: str
    path_template: str
    request_dto: str | None
    response_dto: str
    capability: RiderCapability
    idempotent: bool


RIDER_ROUTE_MANIFEST = (
    RiderRouteContract(
        "rider_availability",
        "PUT",
        "/api/v1/rider/availability",
        "RiderAvailabilityV1",
        "RiderAvailabilityReceiptV1",
        RiderCapability.MANAGE_AVAILABILITY,
        True,
    ),
    RiderRouteContract(
        "rider_offers",
        "GET",
        "/api/v1/rider/offers",
        None,
        "FairOfferListV1",
        RiderCapability.VIEW_OFFERS,
        False,
    ),
    RiderRouteContract(
        "rider_offer_response",
        "POST",
        "/api/v1/rider/offers/{offer_id}/responses",
        "OfferResponseV1",
        "AssignmentV1",
        RiderCapability.RESPOND_OFFER,
        True,
    ),
    RiderRouteContract(
        "rider_progress",
        "POST",
        "/api/v1/rider/assignments/{assignment_id}/progress",
        "DeliveryProgressV1",
        "AssignmentV1",
        RiderCapability.EXECUTE_DELIVERY,
        True,
    ),
    RiderRouteContract(
        "rider_proof_grant",
        "POST",
        "/api/v1/rider/assignments/{assignment_id}/proof-grants",
        "ProofGrantRequestV1",
        "ProofCaptureGrantV1",
        RiderCapability.CAPTURE_PROOF,
        True,
    ),
    RiderRouteContract(
        "rider_delivery",
        "POST",
        "/api/v1/rider/assignments/{assignment_id}/delivery",
        "DeliveryCompletionV1",
        "CompletedEarningV1",
        RiderCapability.EXECUTE_DELIVERY,
        True,
    ),
    RiderRouteContract(
        "rider_earnings",
        "GET",
        "/api/v1/rider/assignments/{assignment_id}/earnings",
        None,
        "EarningsPreviewV1",
        RiderCapability.VIEW_EARNINGS,
        False,
    ),
)
