from __future__ import annotations

from pathlib import Path

import pytest

from narang_rider.persistence import (
    AtomicityViolation,
    ConcurrencyConflict,
    IdempotencyConflict,
    RecordKind,
    SensitiveDataRejected,
    TenantScopeError,
    canonical_payload_digest,
)
from narang_rider.postgres import (
    DatabaseUnavailable,
    PostgresPersistence,
    SerializationRejected,
)


class FakeDatabaseError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class FakeCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.next_row: tuple[object, ...] | None = None
        self.rows: list[tuple[object, ...]] = []
        self.rowcount = 0
        self.fail_with: Exception | None = None

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        if self.fail_with is not None:
            error, self.fail_with = self.fail_with, None
            raise error
        self.executions.append((query, tuple(params)))
        self.next_row = None
        if "RETURNING version" in query:
            self.next_row = (1,)
        if "RETURNING message.record_id" in query:
            self.rows = [("message-1", {"status": "RECEIVED"})]

    def fetchone(self) -> tuple[object, ...] | None:
        row, self.next_row = self.next_row, None
        return row

    def fetchall(self) -> list[tuple[object, ...]]:
        rows, self.rows = self.rows, []
        return rows


class FakeConnection:
    def __init__(self) -> None:
        self.db_cursor = FakeCursor()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.db_cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def adapter_with(connection: FakeConnection) -> PostgresPersistence:
    return PostgresPersistence(lambda: connection)


def valid_digest() -> str:
    return canonical_payload_digest({"command": "create"})


def test_schema_has_tenant_keys_rls_dedupe_and_outbox_lock_contract() -> None:
    schema = Path("migrations/0001_postgres_persistence.sql").read_text()
    for table in (
        "orders",
        "partner_events",
        "rider_calls",
        "ledger_transactions",
        "ledger_entries",
        "outbox_messages",
        "idempotency_records",
        "audit_receipts",
    ):
        assert f"CREATE TABLE {table}" in schema
    assert schema.count("PRIMARY KEY (branch_id,") >= 8
    assert "FORCE ROW LEVEL SECURITY" in schema
    assert "current_setting(''app.branch_id'', true)" in schema
    assert "UNIQUE (branch_id, source_system, source_order_id)" in schema
    assert "UNIQUE (branch_id, order_id)" in schema
    assert "PRIMARY KEY (branch_id, idempotency_key)" in schema
    assert "FOREIGN KEY (branch_id, order_id)" in schema


def test_down_migration_removes_tables_in_dependency_order() -> None:
    down = Path("migrations/0001_postgres_persistence.down.sql").read_text()
    assert down.index("audit_receipts") < down.index("idempotency_records")
    assert down.index("ledger_entries") < down.index("ledger_transactions")
    assert down.strip().endswith("COMMIT;")


def test_atomic_order_bundle_uses_bound_values_and_commits_once() -> None:
    connection = FakeConnection()
    unit = adapter_with(connection).begin(
        branch_id="sejong-1", idempotency_key="order:1", payload_digest=valid_digest()
    )
    unit.put(
        RecordKind.ORDER,
        "order-1",
        {"branch_id": "sejong-1", "source_system": "pos", "source_order_id": "42"},
        expected_version=0,
    )
    unit.put(
        RecordKind.RIDER_CALL,
        "call-1",
        {"branch_id": "sejong-1", "order_id": "order-1", "route": "merchant_direct"},
        expected_version=0,
    )
    unit.put(
        RecordKind.OUTBOX_MESSAGE,
        "status-1",
        {"branch_id": "sejong-1", "requires_ledger": False},
        expected_version=0,
    )

    receipt = unit.commit()

    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert receipt.versions == (1, 1, 1)
    assert all("sejong-1" not in query for query, _ in connection.db_cursor.executions)
    assert any("SET LOCAL app.branch_id = %s" in query for query, _ in connection.db_cursor.executions)
    assert any(params == ("sejong-1",) for _, params in connection.db_cursor.executions)


def test_financial_write_requires_ledger_and_outbox_in_same_transaction() -> None:
    connection = FakeConnection()
    unit = adapter_with(connection).begin(
        branch_id="b", idempotency_key="k", payload_digest=valid_digest()
    )
    unit.put(
        RecordKind.LEDGER_TRANSACTION,
        "tx",
        {"branch_id": "b", "entries": []},
        expected_version=0,
    )
    with pytest.raises(AtomicityViolation):
        unit.commit()
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closed


def test_cross_branch_and_raw_pii_are_rejected_before_sql() -> None:
    connection = FakeConnection()
    unit = adapter_with(connection).begin(
        branch_id="branch-a", idempotency_key="k", payload_digest=valid_digest()
    )
    with pytest.raises(TenantScopeError):
        unit.put(
            RecordKind.ORDER,
            "o",
            {"branch_id": "branch-b"},
            expected_version=0,
        )
    with pytest.raises(SensitiveDataRejected):
        unit.put(
            RecordKind.ORDER,
            "o",
            {"branch_id": "branch-a", "phone": "010-0000-0000"},
            expected_version=0,
        )
    assert connection.db_cursor.executions == []
    unit.rollback()


def test_non_json_payload_is_rejected_before_sql() -> None:
    connection = FakeConnection()
    unit = adapter_with(connection).begin(
        branch_id="b", idempotency_key="k", payload_digest=valid_digest()
    )
    with pytest.raises(SerializationRejected):
        unit.put(
            RecordKind.ORDER,
            "o",
            {"branch_id": "b", "unsupported": object()},
            expected_version=0,
        )
    unit.rollback()


def test_outbox_lease_is_bounded_and_uses_skip_locked() -> None:
    connection = FakeConnection()
    leases = adapter_with(connection).lease_outbox(
        branch_id="b", worker_id="worker-7", limit=10, lease_seconds=20
    )
    query, params = next(
        (query, params)
        for query, params in connection.db_cursor.executions
        if "WITH candidates" in query
    )
    assert "FOR UPDATE SKIP LOCKED LIMIT %s" in query
    assert params[0:3] == ("b", 10, "worker-7")
    assert leases[0].message_id == "message-1"
    assert connection.commits == 1


@pytest.mark.parametrize(
    ("sqlstate", "error_type"),
    [("23503", ConcurrencyConflict), ("23505", ConcurrencyConflict),
     ("40001", ConcurrencyConflict),
     ("42501", TenantScopeError), ("08006", DatabaseUnavailable)],
)
def test_database_errors_have_stable_non_secret_mapping(
    sqlstate: str, error_type: type[Exception]
) -> None:
    connection = FakeConnection()
    connection.db_cursor.fail_with = FakeDatabaseError(sqlstate)
    with pytest.raises(error_type) as captured:
        adapter_with(connection).get(RecordKind.ORDER, "b", "o")
    assert sqlstate not in str(captured.value)
    assert connection.rollbacks == 1
    assert connection.closed


def test_idempotency_replay_and_payload_conflict() -> None:
    class ReplayCursor(FakeCursor):
        def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
            super().execute(query, params)
            if "FROM idempotency_records" in query:
                self.next_row = (
                    valid_digest(), "commit-1", "a" * 64,
                    ["order:o"], [1],
                )

    replay_connection = FakeConnection()
    replay_connection.db_cursor = ReplayCursor()
    unit = adapter_with(replay_connection).begin(
        branch_id="b", idempotency_key="k", payload_digest=valid_digest()
    )
    unit.put(RecordKind.ORDER, "o", {"branch_id": "b"}, expected_version=0)
    assert unit.commit().replayed is True

    conflict_connection = FakeConnection()
    conflict_connection.db_cursor = ReplayCursor()
    conflict = adapter_with(conflict_connection).begin(
        branch_id="b", idempotency_key="k", payload_digest="b" * 64
    )
    conflict.put(RecordKind.ORDER, "o", {"branch_id": "b"}, expected_version=0)
    with pytest.raises(IdempotencyConflict):
        conflict.commit()
    assert conflict_connection.rollbacks == 1


def test_optimistic_conflict_rolls_back_the_whole_transaction() -> None:
    class ConflictCursor(FakeCursor):
        def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
            super().execute(query, params)
            if "RETURNING version" in query:
                self.next_row = None

    connection = FakeConnection()
    connection.db_cursor = ConflictCursor()
    unit = adapter_with(connection).begin(
        branch_id="b", idempotency_key="k", payload_digest=valid_digest()
    )
    unit.put(RecordKind.ORDER, "o", {"branch_id": "b"}, expected_version=2)
    with pytest.raises(ConcurrencyConflict):
        unit.commit()
    assert connection.commits == 0
    assert connection.rollbacks == 1
