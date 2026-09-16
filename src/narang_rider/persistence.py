"""Transactional persistence boundary for NARANG RIDER operations.

The in-memory implementation is a reference adapter.  Production adapters (for
example PostgreSQL) implement the same repository and unit-of-work contracts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol


class PersistenceError(RuntimeError):
    """Base persistence boundary error."""


class TenantScopeError(PersistenceError):
    """A record attempted to cross its branch tenant boundary."""


class ConcurrencyConflict(PersistenceError):
    """The expected record version no longer matches."""


class IdempotencyConflict(PersistenceError):
    """An idempotency key was reused for a different payload."""


class SensitiveDataRejected(PersistenceError):
    """Raw personal data was presented to the persistence boundary."""


class AtomicityViolation(PersistenceError):
    """Financial records were not staged with their required counterpart."""


class LedgerIntegrityViolation(PersistenceError):
    """A persisted ledger payload is not a balanced signed double entry."""


class SimulatedCrash(PersistenceError):
    """Reference-adapter fault used to prove rollback behavior."""


class RecordKind(StrEnum):
    ORDER = "order"
    PARTNER_EVENT = "partner_event"
    RIDER_CALL = "rider_call"
    LEDGER_TRANSACTION = "ledger_transaction"
    OUTBOX_MESSAGE = "outbox_message"


@dataclass(frozen=True)
class StoredRecord:
    kind: RecordKind
    branch_id: str
    record_id: str
    payload: Mapping[str, Any]
    version: int


@dataclass(frozen=True)
class PersistenceAuditReceipt:
    commit_id: str
    branch_id: str
    idempotency_key: str
    payload_digest: str
    record_refs: tuple[str, ...]
    versions: tuple[int, ...]
    audit_hash: str
    replayed: bool = False


class Repository(Protocol):
    """Read side used by services and replaceable storage adapters."""

    def get(self, kind: RecordKind, branch_id: str, record_id: str) -> StoredRecord | None: ...


class UnitOfWork(Protocol):
    """Atomic write boundary used by application services."""

    def put(
        self,
        kind: RecordKind,
        record_id: str,
        payload: Mapping[str, Any],
        *,
        expected_version: int | None,
    ) -> None: ...

    def commit(self) -> PersistenceAuditReceipt: ...

    def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def begin(
        self,
        *,
        branch_id: str,
        idempotency_key: str,
        payload_digest: str,
    ) -> UnitOfWork: ...


@dataclass(frozen=True)
class _PendingWrite:
    kind: RecordKind
    record_id: str
    payload: Mapping[str, Any]
    expected_version: int | None


_RAW_PII_KEYS = {
    "address",
    "delivery_address",
    "email",
    "name",
    "phone",
    "phone_number",
    "recipient_name",
}


def canonical_payload_digest(payload: Mapping[str, Any]) -> str:
    """Return the digest callers bind to an idempotent command."""

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_ledger_payload(payload: Mapping[str, Any]) -> None:
    """Validate the storage representation, independently of domain constructors.

    Ledger entries use a signed amount: positive is debit and negative is credit.
    This boundary is necessary because adapters can be called without constructing a
    :class:`LedgerTransaction` first.
    """

    entries = payload.get("entries")
    if not isinstance(entries, list) or len(entries) < 2:
        raise LedgerIntegrityViolation("ledger requires at least two entries")
    total = 0
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"account_code", "amount_won"}:
            raise LedgerIntegrityViolation("ledger entry shape is invalid")
        account = entry["account_code"]
        amount = entry["amount_won"]
        if not isinstance(account, str) or not account.strip() or len(account) > 128:
            raise LedgerIntegrityViolation("ledger account is invalid")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount == 0:
            raise LedgerIntegrityViolation("ledger amount must be a non-zero integer")
        if not -(2**63) < amount < 2**63:
            raise LedgerIntegrityViolation("ledger amount is outside bigint bounds")
        total += amount
    if total != 0:
        raise LedgerIntegrityViolation("ledger transaction is unbalanced")


def _validate_no_raw_pii(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).lower()
            if normalized in _RAW_PII_KEYS or normalized.endswith("_raw"):
                raise SensitiveDataRejected(f"raw personal data is forbidden at {path}.{key}")
            _validate_no_raw_pii(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _validate_no_raw_pii(nested, f"{path}[{index}]")


class InMemoryPersistence:
    """Thread-safe reference repository and unit-of-work factory."""

    def __init__(self) -> None:
        self._records: dict[tuple[RecordKind, str, str], StoredRecord] = {}
        self._receipts: dict[tuple[str, str], PersistenceAuditReceipt] = {}
        self._lock = threading.RLock()
        self._crash_before_publish = False

    def begin(
        self,
        *,
        branch_id: str,
        idempotency_key: str,
        payload_digest: str,
    ) -> UnitOfWork:
        if not branch_id or not idempotency_key or not payload_digest:
            raise ValueError("branch_id, idempotency_key and payload_digest are required")
        return _InMemoryUnitOfWork(
            adapter=self,
            branch_id=branch_id,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
        )

    def get(self, kind: RecordKind, branch_id: str, record_id: str) -> StoredRecord | None:
        with self._lock:
            record = self._records.get((kind, branch_id, record_id))
            if record is None:
                return None
            return StoredRecord(
                kind=record.kind,
                branch_id=record.branch_id,
                record_id=record.record_id,
                payload=MappingProxyType(copy.deepcopy(dict(record.payload))),
                version=record.version,
            )

    def get_for_branch(
        self,
        kind: RecordKind,
        *,
        requester_branch_id: str,
        owner_branch_id: str,
        record_id: str,
    ) -> StoredRecord | None:
        if requester_branch_id != owner_branch_id:
            raise TenantScopeError("cross-branch read denied")
        return self.get(kind, owner_branch_id, record_id)

    def arm_crash_before_publish(self) -> None:
        """Fail the next commit immediately before its atomic state swap."""

        with self._lock:
            self._crash_before_publish = True


class _InMemoryUnitOfWork:
    def __init__(
        self,
        *,
        adapter: InMemoryPersistence,
        branch_id: str,
        idempotency_key: str,
        payload_digest: str,
    ) -> None:
        self._adapter = adapter
        self._branch_id = branch_id
        self._idempotency_key = idempotency_key
        self._payload_digest = payload_digest
        self._pending: list[_PendingWrite] = []
        self._closed = False

    def put(
        self,
        kind: RecordKind,
        record_id: str,
        payload: Mapping[str, Any],
        *,
        expected_version: int | None,
    ) -> None:
        if self._closed:
            raise PersistenceError("unit of work is closed")
        if not record_id:
            raise ValueError("record_id is required")
        if expected_version is not None and expected_version < 0:
            raise ValueError("expected_version cannot be negative")
        _validate_no_raw_pii(payload)
        if kind is RecordKind.LEDGER_TRANSACTION:
            validate_ledger_payload(payload)
        payload_branch = payload.get("branch_id")
        if payload_branch is not None and payload_branch != self._branch_id:
            raise TenantScopeError("cross-branch write denied")
        if any(item.kind == kind and item.record_id == record_id for item in self._pending):
            raise PersistenceError("duplicate record in one unit of work")
        self._pending.append(
            _PendingWrite(kind, record_id, copy.deepcopy(dict(payload)), expected_version)
        )

    def rollback(self) -> None:
        self._pending.clear()
        self._closed = True

    def commit(self) -> PersistenceAuditReceipt:
        if self._closed:
            raise PersistenceError("unit of work is closed")
        if not self._pending:
            raise PersistenceError("empty transaction cannot commit")
        kinds = {item.kind for item in self._pending}
        if RecordKind.LEDGER_TRANSACTION in kinds and RecordKind.OUTBOX_MESSAGE not in kinds:
            raise AtomicityViolation("ledger transaction requires an outbox message")
        if RecordKind.OUTBOX_MESSAGE in kinds:
            financial_outbox = any(
                item.kind == RecordKind.OUTBOX_MESSAGE
                and item.payload.get("requires_ledger") is True
                for item in self._pending
            )
            if financial_outbox and RecordKind.LEDGER_TRANSACTION not in kinds:
                raise AtomicityViolation("financial outbox requires a ledger transaction")

        adapter = self._adapter
        replay_key = (self._branch_id, self._idempotency_key)
        with adapter._lock:
            previous = adapter._receipts.get(replay_key)
            if previous is not None:
                self._closed = True
                if previous.payload_digest != self._payload_digest:
                    raise IdempotencyConflict("idempotency key payload digest mismatch")
                return PersistenceAuditReceipt(**{**previous.__dict__, "replayed": True})

            next_records = dict(adapter._records)
            written: list[StoredRecord] = []
            for item in self._pending:
                key = (item.kind, self._branch_id, item.record_id)
                current = next_records.get(key)
                current_version = current.version if current is not None else 0
                expected = item.expected_version if item.expected_version is not None else 0
                if current_version != expected:
                    raise ConcurrencyConflict(
                        f"{item.kind}:{item.record_id} expected {expected}, got {current_version}"
                    )
                record = StoredRecord(
                    kind=item.kind,
                    branch_id=self._branch_id,
                    record_id=item.record_id,
                    payload=MappingProxyType(copy.deepcopy(dict(item.payload))),
                    version=current_version + 1,
                )
                next_records[key] = record
                written.append(record)

            audit_material = [
                {
                    "branch_id": record.branch_id,
                    "kind": record.kind.value,
                    "payload": dict(record.payload),
                    "record_id": record.record_id,
                    "version": record.version,
                }
                for record in sorted(written, key=lambda value: (value.kind, value.record_id))
            ]
            audit_hash = canonical_payload_digest({"records": audit_material})
            commit_id = hashlib.sha256(
                f"{self._branch_id}:{self._idempotency_key}:{audit_hash}".encode()
            ).hexdigest()[:24]
            receipt = PersistenceAuditReceipt(
                commit_id=commit_id,
                branch_id=self._branch_id,
                idempotency_key=self._idempotency_key,
                payload_digest=self._payload_digest,
                record_refs=tuple(f"{item.kind.value}:{item.record_id}" for item in written),
                versions=tuple(item.version for item in written),
                audit_hash=audit_hash,
            )
            if adapter._crash_before_publish:
                adapter._crash_before_publish = False
                raise SimulatedCrash("crash before atomic state publication")
            adapter._records = next_records
            adapter._receipts[replay_key] = receipt
            self._closed = True
            return receipt
