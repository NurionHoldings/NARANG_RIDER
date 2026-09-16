"""Reliable, branch-scoped outbox delivery without owning network credentials."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .postgres import ConnectionFactory, _translate_database_error


class RetryableDeliveryError(RuntimeError):
    """The partner may accept the same idempotent delivery later."""


class PermanentDeliveryError(RuntimeError):
    """The partner rejected the event and human review is required."""


class WorkerStopping(RuntimeError):
    """The worker stopped before starting another delivery."""


@dataclass(frozen=True)
class DeliveryEnvelope:
    branch_id: str
    message_id: str
    partner_id: str
    stream_id: str
    sequence: int
    event_type: str
    payload: Mapping[str, Any]
    attempts: int = 0
    available_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))
    lease_owner: str | None = None
    lease_until: datetime | None = None
    delivered_at: datetime | None = None
    dead_lettered_at: datetime | None = None

    @property
    def delivery_key(self) -> str:
        return f"{self.branch_id}:{self.partner_id}:{self.message_id}"


@dataclass(frozen=True)
class DeliveryPolicy:
    batch_size: int = 50
    lease_seconds: int = 30
    max_attempts: int = 5
    base_backoff_seconds: int = 2
    max_backoff_seconds: int = 300
    circuit_failure_threshold: int = 3
    circuit_open_seconds: int = 30

    def __post_init__(self) -> None:
        values = (
            self.batch_size,
            self.lease_seconds,
            self.max_attempts,
            self.base_backoff_seconds,
            self.max_backoff_seconds,
            self.circuit_failure_threshold,
            self.circuit_open_seconds,
        )
        if any(value < 1 for value in values) or self.batch_size > 500:
            raise ValueError("delivery policy values are outside safe bounds")


@dataclass(frozen=True)
class DeliveryMetric:
    name: str
    branch_id: str
    partner_id: str
    message_id: str
    attempts: int


@dataclass(frozen=True)
class ReviewAuditEvent:
    branch_id: str
    message_id: str
    partner_id: str
    reason_code: str
    review_required: bool
    financial_adjustment_allowed: bool
    occurred_at: datetime


class Transport(Protocol):
    def send(
        self,
        *,
        partner_id: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> None: ...


class OutboxStore(Protocol):
    def lease(
        self,
        *,
        branch_id: str,
        worker_id: str,
        limit: int,
        now: datetime,
        lease_until: datetime,
    ) -> tuple[DeliveryEnvelope, ...]: ...

    def ack(self, envelope: DeliveryEnvelope, *, worker_id: str, now: datetime) -> None: ...

    def retry(
        self,
        envelope: DeliveryEnvelope,
        *,
        worker_id: str,
        available_at: datetime,
        reason_code: str,
    ) -> None: ...

    def dead_letter(
        self,
        envelope: DeliveryEnvelope,
        *,
        worker_id: str,
        now: datetime,
        reason_code: str,
    ) -> None: ...

    def release(self, envelope: DeliveryEnvelope, *, worker_id: str, now: datetime) -> None: ...


class InMemoryOutboxStore:
    """Thread-safe reference store with ordering and lease recovery semantics."""

    def __init__(self, messages: tuple[DeliveryEnvelope, ...] = ()) -> None:
        self._messages = {
            (message.branch_id, message.message_id): message for message in messages
        }
        self._audits: list[ReviewAuditEvent] = []
        self._lock = threading.RLock()

    def add(self, message: DeliveryEnvelope) -> None:
        with self._lock:
            key = (message.branch_id, message.message_id)
            if key in self._messages:
                raise ValueError("duplicate outbox message")
            self._messages[key] = message

    def get(self, branch_id: str, message_id: str) -> DeliveryEnvelope:
        with self._lock:
            return self._messages[(branch_id, message_id)]

    @property
    def audits(self) -> tuple[ReviewAuditEvent, ...]:
        with self._lock:
            return tuple(self._audits)

    def lease(
        self,
        *,
        branch_id: str,
        worker_id: str,
        limit: int,
        now: datetime,
        lease_until: datetime,
    ) -> tuple[DeliveryEnvelope, ...]:
        leased: list[DeliveryEnvelope] = []
        with self._lock:
            candidates = sorted(
                self._messages.values(), key=lambda item: (item.available_at, item.sequence)
            )
            for message in candidates:
                if len(leased) >= limit:
                    break
                if not self._eligible(message, branch_id, now):
                    continue
                if self._has_ordering_gap(message):
                    continue
                updated = replace(
                    message,
                    lease_owner=worker_id,
                    lease_until=lease_until,
                    attempts=message.attempts + 1,
                )
                self._messages[(branch_id, message.message_id)] = updated
                leased.append(updated)
        return tuple(leased)

    def _eligible(self, message: DeliveryEnvelope, branch_id: str, now: datetime) -> bool:
        lease_active = message.lease_until is not None and message.lease_until >= now
        return (
            message.branch_id == branch_id
            and message.delivered_at is None
            and message.dead_lettered_at is None
            and message.available_at <= now
            and not lease_active
        )

    def _has_ordering_gap(self, message: DeliveryEnvelope) -> bool:
        return any(
            prior.branch_id == message.branch_id
            and prior.partner_id == message.partner_id
            and prior.stream_id == message.stream_id
            and prior.sequence < message.sequence
            and prior.delivered_at is None
            for prior in self._messages.values()
        )

    def _owned(self, envelope: DeliveryEnvelope, worker_id: str) -> DeliveryEnvelope:
        current = self._messages[(envelope.branch_id, envelope.message_id)]
        if current.lease_owner != worker_id:
            raise RuntimeError("lease ownership lost")
        return current

    def ack(self, envelope: DeliveryEnvelope, *, worker_id: str, now: datetime) -> None:
        with self._lock:
            current = self._owned(envelope, worker_id)
            self._messages[(envelope.branch_id, envelope.message_id)] = replace(
                current, delivered_at=now, lease_owner=None, lease_until=None
            )

    def retry(
        self,
        envelope: DeliveryEnvelope,
        *,
        worker_id: str,
        available_at: datetime,
        reason_code: str,
    ) -> None:
        del reason_code
        with self._lock:
            current = self._owned(envelope, worker_id)
            self._messages[(envelope.branch_id, envelope.message_id)] = replace(
                current,
                available_at=available_at,
                lease_owner=None,
                lease_until=None,
            )

    def dead_letter(
        self,
        envelope: DeliveryEnvelope,
        *,
        worker_id: str,
        now: datetime,
        reason_code: str,
    ) -> None:
        with self._lock:
            current = self._owned(envelope, worker_id)
            self._messages[(envelope.branch_id, envelope.message_id)] = replace(
                current, dead_lettered_at=now, lease_owner=None, lease_until=None
            )
            self._audits.append(
                ReviewAuditEvent(
                    envelope.branch_id,
                    envelope.message_id,
                    envelope.partner_id,
                    reason_code,
                    True,
                    False,
                    now,
                )
            )

    def release(self, envelope: DeliveryEnvelope, *, worker_id: str, now: datetime) -> None:
        with self._lock:
            current = self._owned(envelope, worker_id)
            self._messages[(envelope.branch_id, envelope.message_id)] = replace(
                current, available_at=now, lease_owner=None, lease_until=None
            )


@dataclass
class _Circuit:
    failures: int = 0
    open_until: datetime | None = None


class ReliableOutboxWorker:
    def __init__(
        self,
        *,
        branch_id: str,
        worker_id: str,
        store: OutboxStore,
        transport: Transport,
        policy: DeliveryPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        jitter: Callable[[], float] = lambda: 0.0,
        metric_hook: Callable[[DeliveryMetric], None] = lambda event: None,
    ) -> None:
        if not branch_id or not worker_id:
            raise ValueError("branch and worker identifiers are required")
        self._branch_id = branch_id
        self._worker_id = worker_id
        self._store = store
        self._transport = transport
        self._policy = policy or DeliveryPolicy()
        self._clock = clock
        self._jitter = jitter
        self._metric_hook = metric_hook
        self._circuits: dict[str, _Circuit] = {}
        self._stopping = False

    def request_stop(self) -> None:
        self._stopping = True

    def run_once(self) -> int:
        if self._stopping:
            raise WorkerStopping("worker is stopping")
        now = self._clock()
        envelopes = self._store.lease(
            branch_id=self._branch_id,
            worker_id=self._worker_id,
            limit=self._policy.batch_size,
            now=now,
            lease_until=now + timedelta(seconds=self._policy.lease_seconds),
        )
        completed = 0
        for envelope in envelopes:
            if self._stopping:
                self._store.release(envelope, worker_id=self._worker_id, now=self._clock())
                continue
            if self._circuit_open(envelope.partner_id, now):
                self._store.release(envelope, worker_id=self._worker_id, now=now)
                self._emit("circuit_open", envelope)
                continue
            try:
                self._transport.send(
                    partner_id=envelope.partner_id,
                    payload=envelope.payload,
                    idempotency_key=envelope.delivery_key,
                )
            except PermanentDeliveryError:
                self._dead_letter(envelope, "PARTNER_PERMANENT_REJECTION")
            except RetryableDeliveryError:
                self._retry_or_dead_letter(envelope)
            else:
                self._store.ack(envelope, worker_id=self._worker_id, now=self._clock())
                self._circuits[envelope.partner_id] = _Circuit()
                self._emit("delivered", envelope)
                completed += 1
        return completed

    def _circuit_open(self, partner_id: str, now: datetime) -> bool:
        circuit = self._circuits.setdefault(partner_id, _Circuit())
        if circuit.open_until is not None and circuit.open_until <= now:
            self._circuits[partner_id] = _Circuit()
            return False
        return circuit.open_until is not None

    def _retry_or_dead_letter(self, envelope: DeliveryEnvelope) -> None:
        circuit = self._circuits.setdefault(envelope.partner_id, _Circuit())
        circuit.failures += 1
        if circuit.failures >= self._policy.circuit_failure_threshold:
            circuit.open_until = self._clock() + timedelta(
                seconds=self._policy.circuit_open_seconds
            )
        if envelope.attempts >= self._policy.max_attempts:
            self._dead_letter(envelope, "RETRY_EXHAUSTED")
            return
        exponent = min(envelope.attempts - 1, 30)
        base = min(
            self._policy.max_backoff_seconds,
            self._policy.base_backoff_seconds * (2**exponent),
        )
        jitter_seconds = max(0.0, min(1.0, self._jitter())) * base
        self._store.retry(
            envelope,
            worker_id=self._worker_id,
            available_at=self._clock() + timedelta(seconds=base + jitter_seconds),
            reason_code="PARTNER_RETRYABLE_FAILURE",
        )
        self._emit("retry_scheduled", envelope)

    def _dead_letter(self, envelope: DeliveryEnvelope, reason_code: str) -> None:
        self._store.dead_letter(
            envelope,
            worker_id=self._worker_id,
            now=self._clock(),
            reason_code=reason_code,
        )
        self._emit("human_review_required", envelope)

    def _emit(self, name: str, envelope: DeliveryEnvelope) -> None:
        self._metric_hook(
            DeliveryMetric(
                name,
                envelope.branch_id,
                envelope.partner_id,
                envelope.message_id,
                envelope.attempts,
            )
        )


class PostgresOutboxStore:
    """PostgreSQL lease/ack adapter; every mutation proves lease ownership."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    @staticmethod
    def _scope(cursor: Any, branch_id: str) -> None:
        cursor.execute("SELECT set_config('app.branch_id', %s, true)", (branch_id,))

    def lease(
        self,
        *,
        branch_id: str,
        worker_id: str,
        limit: int,
        now: datetime,
        lease_until: datetime,
    ) -> tuple[DeliveryEnvelope, ...]:
        connection = self._connection_factory()
        try:
            cursor = connection.cursor()
            self._scope(cursor, branch_id)
            cursor.execute(
                """WITH candidates AS (
                    SELECT message.branch_id, message.record_id
                    FROM outbox_messages AS message
                    WHERE message.branch_id = %s
                      AND message.delivered_at IS NULL
                      AND message.dead_lettered_at IS NULL
                      AND message.available_at <= %s
                      AND (message.lease_until IS NULL OR message.lease_until < %s)
                      AND NOT EXISTS (
                          SELECT 1 FROM outbox_messages AS prior
                          WHERE prior.branch_id = message.branch_id
                            AND prior.payload ->> 'partner_id' =
                                message.payload ->> 'partner_id'
                            AND prior.payload ->> 'stream_id' =
                                message.payload ->> 'stream_id'
                            AND (prior.payload ->> 'sequence')::bigint <
                                (message.payload ->> 'sequence')::bigint
                            AND prior.delivered_at IS NULL
                      )
                    ORDER BY message.available_at,
                             (message.payload ->> 'sequence')::bigint
                    FOR UPDATE OF message SKIP LOCKED LIMIT %s
                )
                UPDATE outbox_messages AS message
                SET lease_owner = %s, lease_until = %s,
                    attempts = attempts + 1, updated_at = clock_timestamp()
                FROM candidates
                WHERE message.branch_id = candidates.branch_id
                  AND message.record_id = candidates.record_id
                RETURNING message.record_id, message.payload, message.attempts,
                          message.available_at, message.lease_until""",
                (branch_id, now, now, limit, worker_id, lease_until),
            )
            rows = cursor.fetchall()
            connection.commit()
            return tuple(
                self._envelope(branch_id, worker_id, row) for row in rows
            )
        except Exception as error:
            connection.rollback()
            raise _translate_database_error(error) from error
        finally:
            connection.close()

    @staticmethod
    def _envelope(
        branch_id: str, worker_id: str, row: Any
    ) -> DeliveryEnvelope:
        payload = row[1]
        return DeliveryEnvelope(
            branch_id=branch_id,
            message_id=str(row[0]),
            partner_id=str(payload["partner_id"]),
            stream_id=str(payload["stream_id"]),
            sequence=int(payload["sequence"]),
            event_type=str(payload["event_type"]),
            payload=payload,
            attempts=int(row[2]),
            available_at=row[3],
            lease_owner=worker_id,
            lease_until=row[4],
        )

    def _update_owned(
        self,
        envelope: DeliveryEnvelope,
        worker_id: str,
        assignments: str,
        params: tuple[object, ...],
    ) -> None:
        connection = self._connection_factory()
        try:
            cursor = connection.cursor()
            self._scope(cursor, envelope.branch_id)
            cursor.execute(
                f"UPDATE outbox_messages SET {assignments}, "
                "updated_at = clock_timestamp() "
                "WHERE branch_id = %s AND record_id = %s AND lease_owner = %s",
                (*params, envelope.branch_id, envelope.message_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("lease ownership lost")
            connection.commit()
        except RuntimeError:
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise _translate_database_error(error) from error
        finally:
            connection.close()

    def ack(self, envelope: DeliveryEnvelope, *, worker_id: str, now: datetime) -> None:
        self._update_owned(
            envelope,
            worker_id,
            "delivered_at = %s, lease_owner = NULL, lease_until = NULL",
            (now,),
        )

    def retry(
        self,
        envelope: DeliveryEnvelope,
        *,
        worker_id: str,
        available_at: datetime,
        reason_code: str,
    ) -> None:
        self._update_owned(
            envelope,
            worker_id,
            "available_at = %s, failure_code = %s, lease_owner = NULL, lease_until = NULL",
            (available_at, reason_code),
        )

    def release(self, envelope: DeliveryEnvelope, *, worker_id: str, now: datetime) -> None:
        self._update_owned(
            envelope,
            worker_id,
            "available_at = %s, lease_owner = NULL, lease_until = NULL",
            (now,),
        )

    def dead_letter(
        self,
        envelope: DeliveryEnvelope,
        *,
        worker_id: str,
        now: datetime,
        reason_code: str,
    ) -> None:
        connection = self._connection_factory()
        try:
            cursor = connection.cursor()
            self._scope(cursor, envelope.branch_id)
            cursor.execute(
                "UPDATE outbox_messages SET dead_lettered_at = %s, failure_code = %s, "
                "lease_owner = NULL, lease_until = NULL, updated_at = clock_timestamp() "
                "WHERE branch_id = %s AND record_id = %s AND lease_owner = %s",
                (
                    now,
                    reason_code,
                    envelope.branch_id,
                    envelope.message_id,
                    worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("lease ownership lost")
            cursor.execute(
                "INSERT INTO outbox_review_events "
                "(branch_id, message_id, partner_id, reason_code) "
                "VALUES (%s, %s, %s, %s)",
                (
                    envelope.branch_id,
                    envelope.message_id,
                    envelope.partner_id,
                    reason_code,
                ),
            )
            connection.commit()
        except RuntimeError:
            connection.rollback()
            raise
        except Exception as error:
            connection.rollback()
            raise _translate_database_error(error) from error
        finally:
            connection.close()
