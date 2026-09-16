"""Merchant order operations with tenant, money, and AI authority boundaries."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .application import RiderCallRoute
from .cancellation import (
    CancelActor,
    CancellationPolicy,
    CancellationResult,
    CancelStage,
    cancel,
)
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


class MerchantCapability(StrEnum):
    MANAGE_ORDER = "merchant:order:manage"
    CALL_RIDER = "merchant:rider:call"
    CANCEL_ORDER = "merchant:order:cancel"


class ActorSource(StrEnum):
    HUMAN = "human"
    ARKAON = "arkaon"


class MerchantOrderState(StrEnum):
    DRAFT = "DRAFT"
    QUOTED = "QUOTED"
    SUBMITTED = "SUBMITTED"
    CANCEL_REVIEW = "CANCEL_REVIEW"
    CANCELLED = "CANCELLED"


class DoublePackaging(StrEnum):
    TRUE = "true"
    FALSE = "false"
    NOT_APPLICABLE = "not_applicable"


class MerchantErrorCode(StrEnum):
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    BRANCH_SCOPE_MISMATCH = "BRANCH_SCOPE_MISMATCH"
    CONFLICT = "CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_REQUEST = "INVALID_REQUEST"
    MERCHANT_SCOPE_MISMATCH = "MERCHANT_SCOPE_MISMATCH"
    NOT_FOUND = "NOT_FOUND"
    PRICE_TAMPERED = "PRICE_TAMPERED"
    STALE_QUOTE = "STALE_QUOTE"
    STALE_VERSION = "STALE_VERSION"
    FORBIDDEN_AI_AUTHORITY = "FORBIDDEN_AI_AUTHORITY"


class MerchantCommandRejected(RuntimeError):
    def __init__(self, code: MerchantErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MerchantContext:
    actor_id: str
    merchant_id: str
    branch_id: str
    capabilities: frozenset[MerchantCapability]
    source: ActorSource = ActorSource.HUMAN


@dataclass(frozen=True)
class PublicQuoteSnapshot:
    quote_id: str
    order_id: str
    branch_id: str
    rider_pay_won: int
    merchant_share_won: int
    customer_fee_won: int
    policy_id: str
    valid_until: datetime

    def __post_init__(self) -> None:
        if not all((self.quote_id, self.order_id, self.branch_id, self.policy_id)):
            raise ValueError("quote identity is required")
        if min(self.rider_pay_won, self.merchant_share_won, self.customer_fee_won) < 0:
            raise ValueError("quote money cannot be negative")
        if self.valid_until.tzinfo is None:
            raise ValueError("valid_until must be timezone-aware")

    def public_payload(self) -> dict[str, object]:
        return {
            "quote_id": self.quote_id,
            "order_id": self.order_id,
            "rider_pay_won": self.rider_pay_won,
            "merchant_share_won": self.merchant_share_won,
            "customer_fee_won": self.customer_fee_won,
            "policy_id": self.policy_id,
            "valid_until": self.valid_until.isoformat(),
        }


@dataclass(frozen=True)
class MerchantOrderReceipt:
    order_id: str
    state: MerchantOrderState
    version: int
    replayed: bool


@dataclass(frozen=True)
class CancellationReceipt:
    order_id: str
    state: MerchantOrderState
    result: CancellationResult
    replayed: bool


class MerchantOrderService:
    """Framework-neutral merchant command service.

    Every mutation uses one unit of work for the order, audit event, and outbox.
    The trusted quote is supplied by the server-side pricing boundary, not a DTO.
    """

    def __init__(self, repository: Repository, units: UnitOfWorkFactory) -> None:
        self._repository = repository
        self._units = units

    def create_draft(
        self,
        context: MerchantContext,
        *,
        order_id: str,
        idempotency_key: str,
        prep_minutes_advisory: int | None = None,
    ) -> MerchantOrderReceipt:
        self._require(context, MerchantCapability.MANAGE_ORDER)
        if not order_id or not idempotency_key:
            self._reject(MerchantErrorCode.INVALID_REQUEST, "order and idempotency are required")
        if prep_minutes_advisory is not None and not 0 <= prep_minutes_advisory <= 240:
            self._reject(MerchantErrorCode.INVALID_REQUEST, "invalid prep estimate")
        payload = {
            "branch_id": context.branch_id,
            "merchant_id": context.merchant_id,
            "state": MerchantOrderState.DRAFT.value,
            "prep_minutes_advisory": prep_minutes_advisory,
            "prep_estimate_source": context.source.value,
            "version": 1,
        }
        receipt = self._commit(
            context,
            idempotency_key=f"merchant-draft:{idempotency_key}",
            digest_payload={"order_id": order_id, **payload},
            writes=((RecordKind.ORDER, order_id, payload, 0),),
        )
        return MerchantOrderReceipt(order_id, MerchantOrderState.DRAFT, 1, receipt.replayed)

    def confirm_quote(
        self,
        context: MerchantContext,
        *,
        quote: PublicQuoteSnapshot,
        presented_amounts: tuple[int, int, int],
        expected_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> MerchantOrderReceipt:
        self._human_only(context, "quote confirmation")
        self._require(context, MerchantCapability.MANAGE_ORDER)
        order = self._owned_order(context, quote.order_id)
        replay_candidate = (
            order.version == expected_version + 1
            and order.payload.get("state") == MerchantOrderState.QUOTED.value
            and order.payload.get("quote_id") == quote.quote_id
        )
        if quote.branch_id != context.branch_id:
            self._reject(MerchantErrorCode.BRANCH_SCOPE_MISMATCH, "quote branch mismatch")
        trusted = (quote.rider_pay_won, quote.merchant_share_won, quote.customer_fee_won)
        if presented_amounts != trusted:
            self._reject(
                MerchantErrorCode.PRICE_TAMPERED, "presented money differs from policy quote"
            )
        if now.tzinfo is None or now > quote.valid_until:
            self._reject(MerchantErrorCode.STALE_QUOTE, "quote expired")
        if order.version != expected_version and not replay_candidate:
            self._reject(MerchantErrorCode.STALE_VERSION, "order version changed")
        if not replay_candidate:
            self._state(order.payload, MerchantOrderState.DRAFT)
        order_payload = {**dict(order.payload), "state": MerchantOrderState.QUOTED.value}
        order_payload["quote_id"] = quote.quote_id
        order_payload["version"] = expected_version + 1
        quote_payload = {"branch_id": context.branch_id, **quote.public_payload()}
        receipt = self._commit(
            context,
            idempotency_key=f"merchant-quote:{idempotency_key}",
            digest_payload={"expected_version": expected_version, **quote_payload},
            writes=(
                (RecordKind.ORDER, quote.order_id, order_payload, expected_version),
                (RecordKind.PARTNER_EVENT, f"quote:{quote.quote_id}", quote_payload, 0),
                self._outbox(context, quote.order_id, "QUOTE_CONFIRMED", expected_version + 1),
            ),
        )
        return MerchantOrderReceipt(
            quote.order_id, MerchantOrderState.QUOTED, expected_version + 1, receipt.replayed
        )

    def submit(
        self,
        context: MerchantContext,
        *,
        order_id: str,
        quote_id: str,
        rider_call_route: RiderCallRoute,
        expected_version: int,
        idempotency_key: str,
    ) -> MerchantOrderReceipt:
        self._human_only(context, "order submission and rider call")
        self._require(context, MerchantCapability.MANAGE_ORDER, MerchantCapability.CALL_RIDER)
        order = self._owned_order(context, order_id)
        replay_candidate = (
            order.version == expected_version + 1
            and order.payload.get("state") == MerchantOrderState.SUBMITTED.value
            and order.payload.get("quote_id") == quote_id
            and order.payload.get("rider_call_route") == rider_call_route.value
        )
        if order.version != expected_version and not replay_candidate:
            self._reject(MerchantErrorCode.STALE_VERSION, "order version changed")
        if not replay_candidate:
            self._state(order.payload, MerchantOrderState.QUOTED)
        if order.payload.get("quote_id") != quote_id:
            self._reject(MerchantErrorCode.STALE_QUOTE, "confirmed quote changed")
        quote = self._repository.get(
            RecordKind.PARTNER_EVENT, context.branch_id, f"quote:{quote_id}"
        )
        if quote is None:
            self._reject(MerchantErrorCode.STALE_QUOTE, "immutable quote missing")
        order_payload = {**dict(order.payload), "state": MerchantOrderState.SUBMITTED.value}
        order_payload["rider_call_route"] = rider_call_route.value
        order_payload["version"] = expected_version + 1
        call_payload = {
            "branch_id": context.branch_id,
            "order_id": order_id,
            "route": rider_call_route.value,
            "requested_by": context.actor_id,
        }
        receipt = self._commit(
            context,
            idempotency_key=f"merchant-submit:{idempotency_key}",
            digest_payload={
                "order_id": order_id,
                "quote_id": quote_id,
                "route": rider_call_route.value,
                "expected_version": expected_version,
            },
            writes=(
                (RecordKind.ORDER, order_id, order_payload, expected_version),
                # One global key per order blocks direct/company double calls.
                (RecordKind.RIDER_CALL, order_id, call_payload, 0),
                self._outbox(context, order_id, "ORDER_SUBMITTED", expected_version + 1),
            ),
        )
        return MerchantOrderReceipt(
            order_id, MerchantOrderState.SUBMITTED, expected_version + 1, receipt.replayed
        )

    def record_packaging(
        self,
        context: MerchantContext,
        *,
        order_id: str,
        double_packaging: DoublePackaging,
        seal_number: str | None,
        completed_at: datetime,
        expected_version: int,
        idempotency_key: str,
    ) -> MerchantOrderReceipt:
        self._require(context, MerchantCapability.MANAGE_ORDER)
        order = self._owned_order(context, order_id)
        replay_candidate = (
            order.version == expected_version + 1
            and order.payload.get("double_packaging") == double_packaging.value
            and order.payload.get("seal_number") == seal_number
            and order.payload.get("packaging_completed_at") == completed_at.isoformat()
        )
        if order.version != expected_version and not replay_candidate:
            self._reject(MerchantErrorCode.STALE_VERSION, "order version changed")
        if completed_at.tzinfo is None:
            self._reject(MerchantErrorCode.INVALID_REQUEST, "packaging time needs timezone")
        if seal_number and not self._safe_seal(seal_number):
            self._reject(MerchantErrorCode.INVALID_REQUEST, "seal number is not vault-safe")
        payload = {**dict(order.payload)}
        payload.update(
            {
                "double_packaging": double_packaging.value,
                "seal_number": seal_number,
                "packaging_completed_at": completed_at.isoformat(),
                # Incomplete packaging is never transformed into rider fault.
                "packaging_responsibility": "MERCHANT",
                "rider_fault_inferred": False,
                "version": expected_version + 1,
            }
        )
        receipt = self._commit(
            context,
            idempotency_key=f"merchant-packaging:{idempotency_key}",
            digest_payload={
                "order_id": order_id,
                "double_packaging": double_packaging.value,
                "seal_number": seal_number,
                "completed_at": completed_at.isoformat(),
                "expected_version": expected_version,
            },
            writes=(
                (RecordKind.ORDER, order_id, payload, expected_version),
                self._outbox(context, order_id, "PACKAGING_RECORDED", expected_version + 1),
            ),
        )
        return MerchantOrderReceipt(
            order_id, MerchantOrderState(payload["state"]), expected_version + 1, receipt.replayed
        )

    def cancel_order(
        self,
        context: MerchantContext,
        *,
        order_id: str,
        stage: CancelStage,
        food_handed_over: bool,
        policy: CancellationPolicy,
        expected_version: int,
        idempotency_key: str,
    ) -> CancellationReceipt:
        self._human_only(context, "cancellation")
        self._require(context, MerchantCapability.CANCEL_ORDER)
        order = self._owned_order(context, order_id)
        replay_candidate = order.version == expected_version + 1 and order.payload.get("state") in {
            MerchantOrderState.CANCELLED.value,
            MerchantOrderState.CANCEL_REVIEW.value,
        }
        if order.version != expected_version and not replay_candidate:
            self._reject(MerchantErrorCode.STALE_VERSION, "order version changed")
        result = cancel(
            order_id=order_id,
            actor=CancelActor.MERCHANT,
            stage=stage,
            policy=policy,
            food_handed_over=food_handed_over,
        )
        state = (
            MerchantOrderState.CANCEL_REVIEW
            if result.human_review_required
            else MerchantOrderState.CANCELLED
        )
        payload = {**dict(order.payload), "state": state.value, "version": expected_version + 1}
        event = {
            "branch_id": context.branch_id,
            "order_id": order_id,
            "rider_pay_won": result.rider_pay_won,
            "merchant_charge_won": result.merchant_charge_won,
            "human_review_required": result.human_review_required,
            "rider_penalty_allowed": False,
            "automatic_rider_clawback_allowed": False,
        }
        receipt = self._commit(
            context,
            idempotency_key=f"merchant-cancel:{idempotency_key}",
            digest_payload={"stage": stage.value, "food_handed_over": food_handed_over, **event},
            writes=(
                (RecordKind.ORDER, order_id, payload, expected_version),
                (RecordKind.PARTNER_EVENT, f"cancel:{order_id}:{expected_version + 1}", event, 0),
                self._outbox(context, order_id, state.value, expected_version + 1),
            ),
        )
        return CancellationReceipt(order_id, state, result, receipt.replayed)

    def status(self, context: MerchantContext, order_id: str) -> dict[str, Any]:
        self._require(context, MerchantCapability.MANAGE_ORDER)
        order = self._owned_order(context, order_id)
        return dict(order.payload)

    def _owned_order(self, context: MerchantContext, order_id: str):
        order = self._repository.get(RecordKind.ORDER, context.branch_id, order_id)
        if order is None:
            self._reject(MerchantErrorCode.NOT_FOUND, "order not found in branch")
        if order.payload.get("merchant_id") != context.merchant_id:
            self._reject(
                MerchantErrorCode.MERCHANT_SCOPE_MISMATCH, "order belongs to another merchant"
            )
        return order

    @staticmethod
    def _state(payload: Any, required: MerchantOrderState) -> None:
        if payload.get("state") != required.value:
            MerchantOrderService._reject(MerchantErrorCode.CONFLICT, "invalid order state")

    @staticmethod
    def _require(context: MerchantContext, *required: MerchantCapability) -> None:
        if not set(required).issubset(context.capabilities):
            MerchantOrderService._reject(
                MerchantErrorCode.AUTHORIZATION_DENIED, "capability missing"
            )

    @staticmethod
    def _human_only(context: MerchantContext, action: str) -> None:
        if context.source is ActorSource.ARKAON:
            MerchantOrderService._reject(
                MerchantErrorCode.FORBIDDEN_AI_AUTHORITY,
                f"ARKAON may advise but cannot execute {action}",
            )

    @staticmethod
    def _safe_seal(value: str) -> bool:
        if not 1 <= len(value) <= 64:
            return False
        return all(character.isalnum() or character in "-_" for character in value)

    @staticmethod
    def _outbox(
        context: MerchantContext, order_id: str, event: str, version: int
    ) -> tuple[RecordKind, str, dict[str, object], int]:
        return (
            RecordKind.OUTBOX_MESSAGE,
            f"merchant-order:{order_id}:{version}",
            {
                "branch_id": context.branch_id,
                "order_id": order_id,
                "event": event,
                "sequence": version,
                "requires_ledger": False,
            },
            0,
        )

    def _commit(
        self,
        context: MerchantContext,
        *,
        idempotency_key: str,
        digest_payload: dict[str, object],
        writes: tuple[tuple[RecordKind, str, dict[str, object], int], ...],
    ):
        unit = self._units.begin(
            branch_id=context.branch_id,
            idempotency_key=idempotency_key,
            payload_digest=canonical_payload_digest(digest_payload),
        )
        try:
            for kind, record_id, payload, expected_version in writes:
                unit.put(kind, record_id, payload, expected_version=expected_version)
            return unit.commit()
        except IdempotencyConflict as error:
            unit.rollback()
            raise MerchantCommandRejected(
                MerchantErrorCode.IDEMPOTENCY_CONFLICT, "idempotency payload changed"
            ) from error
        except ConcurrencyConflict as error:
            unit.rollback()
            raise MerchantCommandRejected(
                MerchantErrorCode.STALE_VERSION, "record version changed"
            ) from error
        except (TenantScopeError, SensitiveDataRejected, ValueError) as error:
            unit.rollback()
            raise MerchantCommandRejected(
                MerchantErrorCode.INVALID_REQUEST, "command rejected"
            ) from error

    @staticmethod
    def _reject(code: MerchantErrorCode, message: str) -> None:
        raise MerchantCommandRejected(code, message)


@dataclass(frozen=True)
class MerchantRouteContract:
    route_id: str
    method: str
    path_template: str
    request_dto: str | None
    response_dto: str
    capability: MerchantCapability
    idempotent: bool


MERCHANT_ROUTE_MANIFEST = (
    MerchantRouteContract(
        "merchant_draft",
        "POST",
        "/api/v1/merchant/orders",
        "MerchantDraftV1",
        "MerchantOrderV1",
        MerchantCapability.MANAGE_ORDER,
        True,
    ),
    MerchantRouteContract(
        "merchant_quote_confirm",
        "POST",
        "/api/v1/merchant/orders/{order_id}/quote-confirmations",
        "QuoteConfirmationV1",
        "MerchantOrderV1",
        MerchantCapability.MANAGE_ORDER,
        True,
    ),
    MerchantRouteContract(
        "merchant_submit",
        "POST",
        "/api/v1/merchant/orders/{order_id}/submissions",
        "OrderSubmissionV1",
        "MerchantOrderV1",
        MerchantCapability.CALL_RIDER,
        True,
    ),
    MerchantRouteContract(
        "merchant_packaging",
        "PUT",
        "/api/v1/merchant/orders/{order_id}/packaging",
        "PackagingEvidenceV1",
        "MerchantOrderV1",
        MerchantCapability.MANAGE_ORDER,
        True,
    ),
    MerchantRouteContract(
        "merchant_cancel",
        "POST",
        "/api/v1/merchant/orders/{order_id}/cancellations",
        "MerchantCancellationV1",
        "CancellationReviewV1",
        MerchantCapability.CANCEL_ORDER,
        True,
    ),
    MerchantRouteContract(
        "merchant_status",
        "GET",
        "/api/v1/merchant/orders/{order_id}",
        None,
        "MerchantOrderV1",
        MerchantCapability.MANAGE_ORDER,
        False,
    ),
)


def deterministic_order_id(merchant_id: str, merchant_order_ref: str) -> str:
    """Create a stable non-PII identifier for merchant-side retries."""

    digest = hashlib.sha256(f"{merchant_id}:{merchant_order_ref}".encode()).hexdigest()
    return f"ord_{digest[:24]}"
