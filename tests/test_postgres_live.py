from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from narang_rider.outbox_worker import (
    DeliveryPolicy,
    PermanentDeliveryError,
    PostgresOutboxStore,
    ReliableOutboxWorker,
)
from narang_rider.persistence import (
    ConcurrencyConflict,
    IdempotencyConflict,
    RecordKind,
    canonical_payload_digest,
)
from narang_rider.postgres import PostgresPersistence

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("NARANG_TEST_POSTGRES_DSN")
ADMIN_DSN = os.environ.get("NARANG_TEST_POSTGRES_ADMIN_DSN")
if not DSN or not ADMIN_DSN:
    pytest.skip("live PostgreSQL is only enabled by the dedicated CI job", allow_module_level=True)

UP = Path("migrations/0001_postgres_persistence.sql").read_text()
DOWN = Path("migrations/0001_postgres_persistence.down.sql").read_text()
SETTLEMENT_UP = Path("migrations/0002_settlement_operations.sql").read_text()
SETTLEMENT_DOWN = Path("migrations/0002_settlement_operations.down.sql").read_text()


def connect() -> Any:
    return psycopg.connect(DSN)


def migrate(sql: str) -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as connection:
        connection.execute(sql)


def ensure_application_role() -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'narang_app'"
        ).fetchone()
        if exists is None:
            connection.execute("CREATE ROLE narang_app LOGIN PASSWORD 'narang_app'")


def grant_application_access() -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as connection:
        connection.execute("GRANT USAGE ON SCHEMA public TO narang_app")
        connection.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
            "TO narang_app"
        )


@pytest.fixture(autouse=True)
def fresh_schema() -> None:
    ensure_application_role()
    migrate(SETTLEMENT_DOWN)
    migrate(DOWN)
    migrate(UP)
    migrate(SETTLEMENT_UP)
    grant_application_access()


def digest(command: str) -> str:
    return canonical_payload_digest({"command": command})


def create_order(
    adapter: PostgresPersistence,
    *,
    branch_id: str,
    record_id: str,
    idempotency_key: str,
    source_order_id: str,
) -> None:
    unit = adapter.begin(
        branch_id=branch_id,
        idempotency_key=idempotency_key,
        payload_digest=digest(idempotency_key),
    )
    unit.put(
        RecordKind.ORDER,
        record_id,
        {
            "branch_id": branch_id,
            "source_system": "live-test",
            "source_order_id": source_order_id,
        },
        expected_version=0,
    )
    unit.commit()


def test_migration_fresh_down_up_is_repeatable() -> None:
    migrate(SETTLEMENT_DOWN)
    migrate(DOWN)
    migrate(UP)
    migrate(SETTLEMENT_UP)
    grant_application_access()
    migrate(SETTLEMENT_DOWN)
    migrate(DOWN)
    migrate(UP)
    migrate(SETTLEMENT_UP)
    grant_application_access()
    with connect() as connection:
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert versions == [(1,), (2,)]


def test_rls_hides_other_branches_and_rejects_cross_branch_insert() -> None:
    adapter = PostgresPersistence(connect)
    create_order(
        adapter,
        branch_id="sejong",
        record_id="order-1",
        idempotency_key="create-sejong",
        source_order_id="source-1",
    )

    assert adapter.get(RecordKind.ORDER, "daejeon", "order-1") is None
    with connect() as connection:
        connection.execute("SELECT set_config('app.branch_id', %s, true)", ("daejeon",))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "INSERT INTO orders (branch_id, record_id, payload, version) "
                "VALUES (%s, %s, %s::jsonb, 1)",
                ("sejong", "order-2", "{}"),
            )
        connection.rollback()


def test_composite_fk_and_branch_scoped_source_uniqueness_are_enforced() -> None:
    adapter = PostgresPersistence(connect)
    create_order(
        adapter,
        branch_id="sejong",
        record_id="order-1",
        idempotency_key="first",
        source_order_id="duplicate-source",
    )
    with pytest.raises(ConcurrencyConflict):
        create_order(
            adapter,
            branch_id="sejong",
            record_id="order-2",
            idempotency_key="second",
            source_order_id="duplicate-source",
        )

    with connect() as connection:
        connection.execute("SELECT set_config('app.branch_id', %s, true)", ("daejeon",))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                "INSERT INTO rider_calls "
                "(branch_id, record_id, order_id, payload, version) "
                "VALUES (%s, %s, %s, %s::jsonb, 1)",
                ("daejeon", "call-1", "order-1", "{}"),
            )
        connection.rollback()


def test_serializable_commit_and_optimistic_conflict_use_real_transactions() -> None:
    adapter = PostgresPersistence(connect)
    create_order(
        adapter,
        branch_id="sejong",
        record_id="order-1",
        idempotency_key="create",
        source_order_id="source-1",
    )
    update = adapter.begin(
        branch_id="sejong",
        idempotency_key="update",
        payload_digest=digest("update"),
    )
    update.put(
        RecordKind.ORDER,
        "order-1",
        {
            "branch_id": "sejong",
            "source_system": "live-test",
            "source_order_id": "source-1",
            "status": "DISPATCHED",
        },
        expected_version=1,
    )
    assert update.commit().versions == (2,)

    stale = adapter.begin(
        branch_id="sejong",
        idempotency_key="stale",
        payload_digest=digest("stale"),
    )
    stale.put(
        RecordKind.ORDER,
        "order-1",
        {
            "branch_id": "sejong",
            "source_system": "live-test",
            "source_order_id": "source-1",
            "status": "STALE",
        },
        expected_version=1,
    )
    with pytest.raises(ConcurrencyConflict):
        stale.commit()
    assert adapter.get(RecordKind.ORDER, "sejong", "order-1").version == 2


def test_idempotent_replay_and_digest_conflict_are_persisted() -> None:
    adapter = PostgresPersistence(connect)
    create_order(
        adapter,
        branch_id="sejong",
        record_id="order-1",
        idempotency_key="stable-key",
        source_order_id="source-1",
    )
    replay = adapter.begin(
        branch_id="sejong",
        idempotency_key="stable-key",
        payload_digest=digest("stable-key"),
    )
    replay.put(
        RecordKind.ORDER,
        "order-1",
        {"branch_id": "sejong", "status": "IGNORED"},
        expected_version=99,
    )
    receipt = replay.commit()
    assert receipt.replayed is True
    assert adapter.get(RecordKind.ORDER, "sejong", "order-1").version == 1

    conflict = adapter.begin(
        branch_id="sejong",
        idempotency_key="stable-key",
        payload_digest=digest("different-command"),
    )
    conflict.put(
        RecordKind.ORDER,
        "order-1",
        {"branch_id": "sejong"},
        expected_version=1,
    )
    with pytest.raises(IdempotencyConflict):
        conflict.commit()


def test_ledger_outbox_and_audit_receipt_roll_back_together() -> None:
    adapter = PostgresPersistence(connect)
    unit = adapter.begin(
        branch_id="sejong",
        idempotency_key="financial-failure",
        payload_digest=digest("financial-failure"),
    )
    unit.put(
        RecordKind.LEDGER_TRANSACTION,
        "ledger-1",
        {
            "branch_id": "sejong",
            "entries": [
                {"account_code": "rider-payable", "amount_won": 5000},
                {"account_code": "platform-cash", "amount_won": -5000},
            ],
        },
        expected_version=0,
    )
    unit.put(
        RecordKind.OUTBOX_MESSAGE,
        "outbox-1",
        {"branch_id": "sejong", "requires_ledger": True},
        expected_version=0,
    )
    unit.put(
        RecordKind.RIDER_CALL,
        "invalid-call",
        {"branch_id": "sejong", "order_id": "missing-order"},
        expected_version=0,
    )
    with pytest.raises(ConcurrencyConflict):
        unit.commit()

    with connect() as connection:
        connection.execute("SELECT set_config('app.branch_id', %s, true)", ("sejong",))
        counts = [
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "ledger_transactions",
                "ledger_entries",
                "outbox_messages",
                "idempotency_records",
                "audit_receipts",
            )
        ]
    assert counts == [0, 0, 0, 0, 0]


def test_skip_locked_workers_never_lease_the_same_message() -> None:
    adapter = PostgresPersistence(connect)
    for sequence in range(6):
        unit = adapter.begin(
            branch_id="sejong",
            idempotency_key=f"message-{sequence}",
            payload_digest=digest(f"message-{sequence}"),
        )
        unit.put(
            RecordKind.OUTBOX_MESSAGE,
            f"message-{sequence}",
            {"branch_id": "sejong", "requires_ledger": False},
            expected_version=0,
        )
        unit.commit()

    barrier = Barrier(2)

    def lease(worker_id: str) -> set[str]:
        barrier.wait()
        return {
            item.message_id
            for item in adapter.lease_outbox(
                branch_id="sejong", worker_id=worker_id, limit=3, lease_seconds=30
            )
        }

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(lease, "worker-a")
        second = pool.submit(lease, "worker-b")
    first_ids = first.result()
    second_ids = second.result()
    assert len(first_ids) == len(second_ids) == 3
    assert first_ids.isdisjoint(second_ids)
    assert len(first_ids | second_ids) == 6


def test_postgres_reliable_workers_preserve_order_and_partner_isolation() -> None:
    adapter = PostgresPersistence(connect)
    for partner, stream, sequence in (
        ("partner-a", "order-a", 2),
        ("partner-a", "order-a", 1),
        ("partner-b", "order-b", 1),
    ):
        message_id = f"{partner}-{sequence}"
        unit = adapter.begin(
            branch_id="sejong",
            idempotency_key=message_id,
            payload_digest=digest(message_id),
        )
        unit.put(
            RecordKind.OUTBOX_MESSAGE,
            message_id,
            {
                "branch_id": "sejong",
                "partner_id": partner,
                "stream_id": stream,
                "sequence": sequence,
                "event_type": "RIDER_STATUS",
                "requires_ledger": False,
            },
            expected_version=0,
        )
        unit.commit()

    class Transport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def send(self, *, partner_id: str, payload: Any, idempotency_key: str) -> None:
            del payload
            self.calls.append((partner_id, idempotency_key))

    transport = Transport()
    worker = ReliableOutboxWorker(
        branch_id="sejong",
        worker_id="live-worker",
        store=PostgresOutboxStore(connect),
        transport=transport,
        policy=DeliveryPolicy(batch_size=10),
    )
    assert worker.run_once() == 2
    assert worker.run_once() == 1
    partner_a = [key for partner, key in transport.calls if partner == "partner-a"]
    assert partner_a[0].endswith("partner-a-1")
    assert partner_a[1].endswith("partner-a-2")


def test_postgres_dead_letter_and_review_audit_are_atomic() -> None:
    adapter = PostgresPersistence(connect)
    unit = adapter.begin(
        branch_id="sejong",
        idempotency_key="dead-letter-message",
        payload_digest=digest("dead-letter-message"),
    )
    unit.put(
        RecordKind.OUTBOX_MESSAGE,
        "dead-letter-message",
        {
            "branch_id": "sejong",
            "partner_id": "partner-a",
            "stream_id": "financial-order-1",
            "sequence": 1,
            "event_type": "FINANCIAL_STATUS",
            "requires_ledger": False,
        },
        expected_version=0,
    )
    unit.commit()

    class RejectingTransport:
        def send(self, *, partner_id: str, payload: Any, idempotency_key: str) -> None:
            del partner_id, payload, idempotency_key
            raise PermanentDeliveryError("contract rejected")

    worker = ReliableOutboxWorker(
        branch_id="sejong",
        worker_id="live-worker",
        store=PostgresOutboxStore(connect),
        transport=RejectingTransport(),
    )
    assert worker.run_once() == 0

    with connect() as connection:
        connection.execute("SELECT set_config('app.branch_id', %s, true)", ("sejong",))
        outbox = connection.execute(
            "SELECT delivered_at, dead_lettered_at FROM outbox_messages "
            "WHERE branch_id = %s AND record_id = %s",
            ("sejong", "dead-letter-message"),
        ).fetchone()
        audit = connection.execute(
            "SELECT review_required, financial_adjustment_allowed "
            "FROM outbox_review_events WHERE branch_id = %s AND message_id = %s",
            ("sejong", "dead-letter-message"),
        ).fetchone()
    assert outbox[0] is None and outbox[1] is not None
    assert audit == (True, False)
