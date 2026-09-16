"""PostgreSQL 16 adapter for the persistence contracts.

The adapter accepts a DB-API compatible connection factory so deployment owns
pooling and credentials. SQL identifiers are selected only from a closed enum;
all runtime values are bound parameters.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .persistence import (
    AtomicityViolation,
    ConcurrencyConflict,
    IdempotencyConflict,
    PersistenceAuditReceipt,
    PersistenceError,
    RecordKind,
    StoredRecord,
    TenantScopeError,
    UnitOfWork,
    _validate_no_raw_pii,
    canonical_payload_digest,
)


class Cursor(Protocol):
    rowcount: int

    def execute(self, query: str, params: Sequence[object] = ()) -> Any: ...

    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> list[Sequence[object]]: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[], Connection]

_TABLES = {
    RecordKind.ORDER: "orders",
    RecordKind.PARTNER_EVENT: "partner_events",
    RecordKind.RIDER_CALL: "rider_calls",
    RecordKind.LEDGER_TRANSACTION: "ledger_transactions",
    RecordKind.OUTBOX_MESSAGE: "outbox_messages",
}


class DatabaseUnavailable(PersistenceError):
    """The database could not complete an otherwise retryable operation."""


class SerializationRejected(PersistenceError):
    """A payload is not a JSON object or contains unsupported values."""


@dataclass(frozen=True)
class OutboxLease:
    branch_id: str
    message_id: str
    payload: Mapping[str, Any]
    lease_owner: str
    lease_until: datetime


@dataclass(frozen=True)
class _Write:
    kind: RecordKind
    record_id: str
    payload: dict[str, Any]
    expected_version: int


def _json_object(payload: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    _validate_no_raw_pii(payload)
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise SerializationRejected("payload must be finite JSON data") from error
    if not isinstance(decoded, dict):
        raise SerializationRejected("payload must be a JSON object")
    return decoded, encoded


def _translate_database_error(error: Exception) -> PersistenceError:
    code = getattr(error, "sqlstate", None) or getattr(error, "pgcode", None)
    if code in {"23503", "23505"}:
        return ConcurrencyConflict("storage constraint rejected the write")
    if code == "40001":
        return ConcurrencyConflict("database serialization conflict")
    if code == "42501":
        return TenantScopeError("database branch policy denied access")
    return DatabaseUnavailable("database operation failed")


class PostgresPersistence:
    """Repository and unit-of-work factory backed by PostgreSQL."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def begin(
        self, *, branch_id: str, idempotency_key: str, payload_digest: str
    ) -> UnitOfWork:
        if not branch_id or not idempotency_key or len(payload_digest) != 64:
            raise ValueError("valid branch, idempotency key and SHA-256 digest are required")
        return _PostgresUnitOfWork(
            factory=self._connection_factory,
            branch_id=branch_id,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
        )

    def get(self, kind: RecordKind, branch_id: str, record_id: str) -> StoredRecord | None:
        connection = self._connection_factory()
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT set_config('app.branch_id', %s, true)", (branch_id,))
            cursor.execute(
                f"SELECT payload, version FROM {_TABLES[kind]} "
                "WHERE branch_id = %s AND record_id = %s",
                (branch_id, record_id),
            )
            row = cursor.fetchone()
            connection.commit()
            if row is None:
                return None
            payload = row[0] if isinstance(row[0], Mapping) else json.loads(str(row[0]))
            return StoredRecord(kind, branch_id, record_id, payload, int(row[1]))
        except PersistenceError:
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise _translate_database_error(error) from error
        finally:
            connection.close()

    def lease_outbox(
        self,
        *,
        branch_id: str,
        worker_id: str,
        limit: int = 50,
        lease_seconds: int = 30,
    ) -> tuple[OutboxLease, ...]:
        if not branch_id or not worker_id or not 1 <= limit <= 500 or lease_seconds < 1:
            raise ValueError("invalid outbox lease request")
        connection = self._connection_factory()
        lease_until = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT set_config('app.branch_id', %s, true)", (branch_id,))
            cursor.execute(
                """WITH candidates AS (
                    SELECT branch_id, record_id FROM outbox_messages
                    WHERE branch_id = %s AND delivered_at IS NULL
                      AND available_at <= clock_timestamp()
                      AND (lease_until IS NULL OR lease_until < clock_timestamp())
                    ORDER BY available_at, created_at
                    FOR UPDATE SKIP LOCKED LIMIT %s
                )
                UPDATE outbox_messages AS message
                SET lease_owner = %s, lease_until = %s, attempts = attempts + 1,
                    updated_at = clock_timestamp()
                FROM candidates
                WHERE message.branch_id = candidates.branch_id
                  AND message.record_id = candidates.record_id
                RETURNING message.record_id, message.payload""",
                (branch_id, limit, worker_id, lease_until),
            )
            rows = cursor.fetchall()
            connection.commit()
            return tuple(
                OutboxLease(branch_id, str(row[0]), row[1], worker_id, lease_until)
                for row in rows
            )
        except Exception as error:
            connection.rollback()
            raise _translate_database_error(error) from error
        finally:
            connection.close()


class _PostgresUnitOfWork:
    def __init__(
        self,
        *,
        factory: ConnectionFactory,
        branch_id: str,
        idempotency_key: str,
        payload_digest: str,
    ) -> None:
        self._connection = factory()
        self._branch_id = branch_id
        self._idempotency_key = idempotency_key
        self._payload_digest = payload_digest
        self._writes: list[_Write] = []
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
        if not record_id or expected_version is not None and expected_version < 0:
            raise ValueError("valid record id and expected version are required")
        normalized, _ = _json_object(payload)
        if normalized.get("branch_id", self._branch_id) != self._branch_id:
            raise TenantScopeError("cross-branch write denied")
        if any(write.kind == kind and write.record_id == record_id for write in self._writes):
            raise PersistenceError("duplicate record in unit of work")
        self._writes.append(_Write(kind, record_id, normalized, expected_version or 0))

    def rollback(self) -> None:
        if not self._closed:
            self._connection.rollback()
            self._connection.close()
            self._closed = True

    def commit(self) -> PersistenceAuditReceipt:
        if self._closed or not self._writes:
            raise PersistenceError("unit of work is closed or empty")
        kinds = {write.kind for write in self._writes}
        if RecordKind.LEDGER_TRANSACTION in kinds and RecordKind.OUTBOX_MESSAGE not in kinds:
            self.rollback()
            raise AtomicityViolation("ledger transaction requires outbox")
        if any(
            write.kind == RecordKind.OUTBOX_MESSAGE and write.payload.get("requires_ledger") is True
            for write in self._writes
        ) and RecordKind.LEDGER_TRANSACTION not in kinds:
            self.rollback()
            raise AtomicityViolation("financial outbox requires ledger transaction")
        try:
            receipt = self._commit_transaction()
            self._connection.commit()
            self._closed = True
            self._connection.close()
            return receipt
        except PersistenceError:
            self.rollback()
            raise
        except Exception as error:
            self.rollback()
            raise _translate_database_error(error) from error

    def _commit_transaction(self) -> PersistenceAuditReceipt:
        cursor = self._connection.cursor()
        cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        cursor.execute("SELECT set_config('app.branch_id', %s, true)", (self._branch_id,))
        cursor.execute(
            "SELECT payload_digest, commit_id, audit_hash, record_refs, versions "
            "FROM idempotency_records WHERE branch_id = %s AND idempotency_key = %s "
            "FOR UPDATE",
            (self._branch_id, self._idempotency_key),
        )
        replay = cursor.fetchone()
        if replay is not None:
            if replay[0] != self._payload_digest:
                raise IdempotencyConflict("idempotency digest mismatch")
            return PersistenceAuditReceipt(
                str(replay[1]), self._branch_id, self._idempotency_key,
                self._payload_digest, tuple(replay[3]), tuple(replay[4]), str(replay[2]), True,
            )

        versions: list[int] = []
        refs: list[str] = []
        audit_records: list[dict[str, Any]] = []
        for write in self._writes:
            version = self._write_record(cursor, write)
            versions.append(version)
            refs.append(f"{write.kind.value}:{write.record_id}")
            audit_records.append(
                {"branch_id": self._branch_id, "kind": write.kind.value,
                 "payload": write.payload, "record_id": write.record_id, "version": version}
            )
        audit_hash = canonical_payload_digest(
            {"records": sorted(audit_records, key=lambda item: (item["kind"], item["record_id"]))}
        )
        commit_id = hashlib.sha256(
            f"{self._branch_id}:{self._idempotency_key}:{audit_hash}".encode()
        ).hexdigest()[:24]
        params = (
            self._branch_id, self._idempotency_key, self._payload_digest,
            commit_id, audit_hash, json.dumps(refs), json.dumps(versions),
        )
        cursor.execute(
            "INSERT INTO idempotency_records "
            "(branch_id, idempotency_key, payload_digest, commit_id, audit_hash, "
            "record_refs, versions) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)",
            params,
        )
        cursor.execute(
            "INSERT INTO audit_receipts "
            "(branch_id, commit_id, idempotency_key, payload_digest, audit_hash, "
            "record_refs, versions) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)",
            (self._branch_id, commit_id, self._idempotency_key, self._payload_digest,
             audit_hash, json.dumps(refs), json.dumps(versions)),
        )
        return PersistenceAuditReceipt(
            commit_id, self._branch_id, self._idempotency_key, self._payload_digest,
            tuple(refs), tuple(versions), audit_hash,
        )

    def _write_record(self, cursor: Cursor, write: _Write) -> int:
        table = _TABLES[write.kind]
        payload_json = json.dumps(write.payload, ensure_ascii=False, sort_keys=True)
        if write.kind == RecordKind.RIDER_CALL:
            cursor.execute(
                f"INSERT INTO {table} (branch_id, record_id, order_id, payload, version) "
                "VALUES (%s, %s, %s, %s::jsonb, 1) ON CONFLICT (branch_id, record_id) "
                "DO UPDATE SET payload = EXCLUDED.payload, version = "
                f"{table}.version + 1, updated_at = clock_timestamp() "
                f"WHERE {table}.version = %s RETURNING version",
                (self._branch_id, write.record_id, write.payload.get("order_id"),
                 payload_json, write.expected_version),
            )
        else:
            cursor.execute(
                f"INSERT INTO {table} (branch_id, record_id, payload, version) "
                "VALUES (%s, %s, %s::jsonb, 1) ON CONFLICT (branch_id, record_id) "
                "DO UPDATE SET payload = EXCLUDED.payload, version = "
                f"{table}.version + 1, updated_at = clock_timestamp() "
                f"WHERE {table}.version = %s RETURNING version",
                (self._branch_id, write.record_id, payload_json, write.expected_version),
            )
        row = cursor.fetchone()
        if row is None:
            raise ConcurrencyConflict(
                f"{write.kind.value}:{write.record_id} optimistic version mismatch"
            )
        if write.kind == RecordKind.LEDGER_TRANSACTION:
            self._write_ledger_entries(cursor, write)
        return int(row[0])

    def _write_ledger_entries(self, cursor: Cursor, write: _Write) -> None:
        entries = write.payload.get("entries", [])
        if not isinstance(entries, list):
            raise SerializationRejected("ledger entries must be a list")
        for sequence, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise SerializationRejected("ledger entry must be an object")
            cursor.execute(
                "INSERT INTO ledger_entries "
                "(branch_id, transaction_id, entry_sequence, account_code, amount_won) "
                "VALUES (%s, %s, %s, %s, %s)",
                (self._branch_id, write.record_id, sequence,
                 entry.get("account_code"), entry.get("amount_won")),
            )
