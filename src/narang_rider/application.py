"""Framework-neutral order intake application service."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .persistence import (
    ConcurrencyConflict,
    IdempotencyConflict,
    PersistenceAuditReceipt,
    RecordKind,
    SensitiveDataRejected,
    SimulatedCrash,
    TenantScopeError,
    UnitOfWorkFactory,
    canonical_payload_digest,
)


class CommandCapability(StrEnum):
    INGEST_ORDER = "order:ingest"
    CALL_RIDER = "rider:call"


class RiderCallRoute(StrEnum):
    MERCHANT_DIRECT = "merchant_direct"
    RIDER_COMPANY = "rider_company"


class IntakeErrorCode(StrEnum):
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    BRANCH_SCOPE_MISMATCH = "BRANCH_SCOPE_MISMATCH"
    CONCURRENT_CONFLICT = "CONCURRENT_CONFLICT"
    DUPLICATE_RIDER_CALL = "DUPLICATE_RIDER_CALL"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_REQUEST = "INVALID_REQUEST"
    PII_POLICY_VIOLATION = "PII_POLICY_VIOLATION"
    RETRYABLE_PERSISTENCE_FAILURE = "RETRYABLE_PERSISTENCE_FAILURE"


class IntakeCommandRejected(RuntimeError):
    """Stable framework-neutral error returned by the application boundary."""

    def __init__(self, code: IntakeErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CommandContext:
    actor_id: str
    branch_id: str
    capabilities: frozenset[CommandCapability]

    def __post_init__(self) -> None:
        if not self.actor_id or not self.branch_id:
            raise ValueError("actor_id and branch_id are required")


@dataclass(frozen=True)
class OrderIntakeCommand:
    source_system: str
    source_order_id: str
    idempotency_key: str
    branch_id: str
    merchant_id: str
    total_won: int
    delivery_address_vault_ref: str
    recipient_phone_vault_ref: str
    rider_call_route: RiderCallRoute
    attributes: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        required = (
            self.source_system,
            self.source_order_id,
            self.idempotency_key,
            self.branch_id,
            self.merchant_id,
        )
        if not all(required):
            raise ValueError("required intake field is empty")
        if self.total_won < 0:
            raise ValueError("total_won cannot be negative")
        for reference in (
            self.delivery_address_vault_ref,
            self.recipient_phone_vault_ref,
        ):
            if not re.fullmatch(r"vault://[A-Za-z0-9._~:/-]+", reference):
                raise ValueError("personal data must be represented by a vault reference")


@dataclass(frozen=True)
class OrderIntakeReceipt:
    order_id: str
    branch_id: str
    source_system: str
    source_order_id: str
    rider_call_route: RiderCallRoute
    status_sequence: int
    persistence_receipt: PersistenceAuditReceipt


class OrderIntakeApplicationService:
    """Canonicalizes and commits one intake command as a single transaction."""

    def __init__(self, persistence: UnitOfWorkFactory) -> None:
        self._persistence = persistence

    def ingest(self, context: CommandContext, command: OrderIntakeCommand) -> OrderIntakeReceipt:
        self._authorize(context, command)
        order_id = self._canonical_order_id(command)
        command_payload = self._command_payload(command, order_id)
        digest = canonical_payload_digest(command_payload)
        unit = self._persistence.begin(
            branch_id=command.branch_id,
            idempotency_key=f"order-intake:{command.source_system}:{command.idempotency_key}",
            payload_digest=digest,
        )
        try:
            unit.put(
                RecordKind.ORDER,
                order_id,
                {
                    "branch_id": command.branch_id,
                    "merchant_id": command.merchant_id,
                    "source_system": command.source_system,
                    "source_order_id": command.source_order_id,
                    "total_won": command.total_won,
                    "delivery_address_vault_ref": command.delivery_address_vault_ref,
                    "recipient_phone_vault_ref": command.recipient_phone_vault_ref,
                    "attributes": dict(command.attributes or {}),
                    "state": "RECEIVED",
                },
                expected_version=0,
            )
            unit.put(
                RecordKind.PARTNER_EVENT,
                self._source_event_id(command),
                {
                    "branch_id": command.branch_id,
                    "order_id": order_id,
                    "source_system": command.source_system,
                    "source_order_id": command.source_order_id,
                    "idempotency_key": command.idempotency_key,
                    "payload_digest": digest,
                },
                expected_version=0,
            )
            unit.put(
                RecordKind.RIDER_CALL,
                order_id,
                {
                    "branch_id": command.branch_id,
                    "order_id": order_id,
                    "route": command.rider_call_route.value,
                    "requested_by": context.actor_id,
                },
                expected_version=0,
            )
            unit.put(
                RecordKind.OUTBOX_MESSAGE,
                f"order-status:{order_id}:1",
                {
                    "branch_id": command.branch_id,
                    "order_id": order_id,
                    "source_system": command.source_system,
                    "status": "RECEIVED",
                    "sequence": 1,
                    "requires_ledger": False,
                },
                expected_version=0,
            )
            persisted = unit.commit()
        except (
            ConcurrencyConflict,
            IdempotencyConflict,
            SensitiveDataRejected,
            SimulatedCrash,
            TenantScopeError,
            ValueError,
        ) as error:
            unit.rollback()
            self._raise_stable_error(error)
            raise AssertionError("unreachable")

        return OrderIntakeReceipt(
            order_id=order_id,
            branch_id=command.branch_id,
            source_system=command.source_system,
            source_order_id=command.source_order_id,
            rider_call_route=command.rider_call_route,
            status_sequence=1,
            persistence_receipt=persisted,
        )

    @staticmethod
    def _authorize(context: CommandContext, command: OrderIntakeCommand) -> None:
        required = {CommandCapability.INGEST_ORDER, CommandCapability.CALL_RIDER}
        if not required.issubset(context.capabilities):
            raise IntakeCommandRejected(
                IntakeErrorCode.AUTHORIZATION_DENIED,
                "actor lacks order intake capabilities",
            )
        if context.branch_id != command.branch_id:
            raise IntakeCommandRejected(
                IntakeErrorCode.BRANCH_SCOPE_MISMATCH,
                "actor cannot ingest an order for another branch",
            )

    @staticmethod
    def _canonical_order_id(command: OrderIntakeCommand) -> str:
        identity = f"{command.source_system}:{command.source_order_id}".encode()
        return f"ord_{hashlib.sha256(identity).hexdigest()[:24]}"

    @staticmethod
    def _source_event_id(command: OrderIntakeCommand) -> str:
        identity = f"{command.source_system}:{command.source_order_id}".encode()
        return f"source_{hashlib.sha256(identity).hexdigest()[:24]}"

    @staticmethod
    def _command_payload(command: OrderIntakeCommand, order_id: str) -> dict[str, object]:
        return {
            "attributes": dict(command.attributes or {}),
            "branch_id": command.branch_id,
            "delivery_address_vault_ref": command.delivery_address_vault_ref,
            "merchant_id": command.merchant_id,
            "order_id": order_id,
            "recipient_phone_vault_ref": command.recipient_phone_vault_ref,
            "rider_call_route": command.rider_call_route.value,
            "source_order_id": command.source_order_id,
            "source_system": command.source_system,
            "total_won": command.total_won,
        }

    @staticmethod
    def _raise_stable_error(error: Exception) -> None:
        if isinstance(error, IdempotencyConflict):
            raise IntakeCommandRejected(
                IntakeErrorCode.IDEMPOTENCY_CONFLICT, "idempotency payload changed"
            ) from error
        if isinstance(error, SensitiveDataRejected):
            raise IntakeCommandRejected(
                IntakeErrorCode.PII_POLICY_VIOLATION, "raw personal data is forbidden"
            ) from error
        if isinstance(error, TenantScopeError):
            raise IntakeCommandRejected(
                IntakeErrorCode.BRANCH_SCOPE_MISMATCH, "branch scope mismatch"
            ) from error
        if isinstance(error, SimulatedCrash):
            raise IntakeCommandRejected(
                IntakeErrorCode.RETRYABLE_PERSISTENCE_FAILURE,
                "persistence failed before publication",
            ) from error
        if isinstance(error, ConcurrencyConflict):
            kind = IntakeErrorCode.CONCURRENT_CONFLICT
            if "rider_call" in str(error):
                kind = IntakeErrorCode.DUPLICATE_RIDER_CALL
            raise IntakeCommandRejected(kind, "intake record already exists") from error
        if isinstance(error, ValueError):
            raise IntakeCommandRejected(IntakeErrorCode.INVALID_REQUEST, str(error)) from error
        raise error
