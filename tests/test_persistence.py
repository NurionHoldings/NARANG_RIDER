from concurrent.futures import ThreadPoolExecutor

import pytest

from narang_rider.persistence import (
    AtomicityViolation,
    ConcurrencyConflict,
    IdempotencyConflict,
    InMemoryPersistence,
    RecordKind,
    SensitiveDataRejected,
    SimulatedCrash,
    TenantScopeError,
    canonical_payload_digest,
)

BRANCH = "branch-sejong-01"


def _begin(store: InMemoryPersistence, key: str, payload: dict | None = None):
    command = payload or {"command": key}
    return store.begin(
        branch_id=BRANCH,
        idempotency_key=key,
        payload_digest=canonical_payload_digest(command),
    )


def _stage_financial_bundle(unit, suffix: str = "1") -> None:
    unit.put(
        RecordKind.LEDGER_TRANSACTION,
        f"ledger-{suffix}",
        {
            "branch_id": BRANCH,
            "entries": [
                {"account_code": "RIDER_EARNING_EXPENSE", "amount_won": 4_000},
                {"account_code": "RIDER_PAYABLE", "amount_won": -4_000},
            ],
        },
        expected_version=0,
    )
    unit.put(
        RecordKind.OUTBOX_MESSAGE,
        f"outbox-{suffix}",
        {"branch_id": BRANCH, "requires_ledger": True, "topic": "settlement.created"},
        expected_version=0,
    )


def test_commits_operational_bundle_atomically_with_audit_receipt() -> None:
    store = InMemoryPersistence()
    unit = _begin(store, "create-order-1")
    unit.put(
        RecordKind.ORDER,
        "order-1",
        {"branch_id": BRANCH, "delivery_address_vault_ref": "vault://pii/address/1"},
        expected_version=0,
    )
    unit.put(
        RecordKind.PARTNER_EVENT,
        "partner-event-1",
        {"branch_id": BRANCH, "source": "ai-baebi"},
        expected_version=0,
    )
    unit.put(
        RecordKind.RIDER_CALL,
        "rider-call-1",
        {"branch_id": BRANCH, "route": "merchant_direct"},
        expected_version=0,
    )
    _stage_financial_bundle(unit)

    receipt = unit.commit()

    assert receipt.branch_id == BRANCH
    assert len(receipt.record_refs) == 5
    assert receipt.versions == (1, 1, 1, 1, 1)
    assert len(receipt.audit_hash) == 64
    assert store.get(RecordKind.ORDER, BRANCH, "order-1").version == 1
    assert store.get(RecordKind.OUTBOX_MESSAGE, BRANCH, "outbox-1") is not None


def test_explicit_rollback_publishes_nothing() -> None:
    store = InMemoryPersistence()
    unit = _begin(store, "rollback-1")
    unit.put(RecordKind.ORDER, "order-1", {"branch_id": BRANCH}, expected_version=0)
    unit.rollback()

    assert store.get(RecordKind.ORDER, BRANCH, "order-1") is None


def test_crash_before_publish_rolls_back_and_retry_can_commit() -> None:
    store = InMemoryPersistence()
    store.arm_crash_before_publish()
    failed = _begin(store, "crash-1")
    _stage_financial_bundle(failed)

    with pytest.raises(SimulatedCrash):
        failed.commit()

    assert store.get(RecordKind.LEDGER_TRANSACTION, BRANCH, "ledger-1") is None
    retry = _begin(store, "crash-1")
    _stage_financial_bundle(retry)
    assert retry.commit().replayed is False


def test_same_idempotency_key_and_digest_replays_receipt() -> None:
    store = InMemoryPersistence()
    first = _begin(store, "retry-1")
    first.put(RecordKind.ORDER, "order-1", {"branch_id": BRANCH}, expected_version=0)
    first_receipt = first.commit()

    retry = _begin(store, "retry-1")
    retry.put(RecordKind.ORDER, "order-1", {"branch_id": BRANCH}, expected_version=0)
    replay = retry.commit()

    assert replay.commit_id == first_receipt.commit_id
    assert replay.replayed is True
    assert store.get(RecordKind.ORDER, BRANCH, "order-1").version == 1


def test_idempotency_key_payload_change_fails_closed() -> None:
    store = InMemoryPersistence()
    first = _begin(store, "same-key", {"amount": 1})
    first.put(RecordKind.ORDER, "order-1", {"branch_id": BRANCH}, expected_version=0)
    first.commit()

    attack = _begin(store, "same-key", {"amount": 999_999})
    attack.put(RecordKind.ORDER, "order-2", {"branch_id": BRANCH}, expected_version=0)
    with pytest.raises(IdempotencyConflict):
        attack.commit()

    assert store.get(RecordKind.ORDER, BRANCH, "order-2") is None


def test_optimistic_version_check_blocks_stale_writer() -> None:
    store = InMemoryPersistence()
    create = _begin(store, "order-create")
    create.put(RecordKind.ORDER, "order-1", {"branch_id": BRANCH}, expected_version=0)
    create.commit()

    update = _begin(store, "order-update")
    update.put(
        RecordKind.ORDER,
        "order-1",
        {"branch_id": BRANCH, "state": "ACCEPTED"},
        expected_version=1,
    )
    assert update.commit().versions == (2,)

    stale = _begin(store, "order-stale")
    stale.put(
        RecordKind.ORDER,
        "order-1",
        {"branch_id": BRANCH, "state": "CANCELLED"},
        expected_version=1,
    )
    with pytest.raises(ConcurrencyConflict):
        stale.commit()


def test_concurrent_compare_and_set_allows_only_one_writer() -> None:
    store = InMemoryPersistence()
    create = _begin(store, "concurrent-create")
    create.put(RecordKind.ORDER, "order-1", {"branch_id": BRANCH}, expected_version=0)
    create.commit()

    def update(index: int) -> str:
        unit = _begin(store, f"concurrent-{index}")
        unit.put(
            RecordKind.ORDER,
            "order-1",
            {"branch_id": BRANCH, "winner": index},
            expected_version=1,
        )
        try:
            unit.commit()
            return "committed"
        except ConcurrencyConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(update, range(8)))

    assert outcomes.count("committed") == 1
    assert outcomes.count("conflict") == 7
    assert store.get(RecordKind.ORDER, BRANCH, "order-1").version == 2


def test_cross_branch_payload_write_is_denied() -> None:
    store = InMemoryPersistence()
    unit = _begin(store, "tenant-attack")
    with pytest.raises(TenantScopeError):
        unit.put(
            RecordKind.ORDER,
            "order-1",
            {"branch_id": "branch-busan-01"},
            expected_version=0,
        )


def test_cross_branch_read_is_denied_even_when_record_exists() -> None:
    store = InMemoryPersistence()
    unit = _begin(store, "tenant-create")
    unit.put(RecordKind.ORDER, "order-1", {"branch_id": BRANCH}, expected_version=0)
    unit.commit()

    with pytest.raises(TenantScopeError):
        store.get_for_branch(
            RecordKind.ORDER,
            requester_branch_id="branch-busan-01",
            owner_branch_id=BRANCH,
            record_id="order-1",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"address": "세종시 어느로 1"},
        {"customer": {"phone": "010-0000-0000"}},
        {"email": "person@example.test"},
        {"recipient_name": "홍길동"},
    ],
)
def test_raw_personal_data_is_rejected(payload: dict) -> None:
    store = InMemoryPersistence()
    unit = _begin(store, "pii-rejected")
    with pytest.raises(SensitiveDataRejected):
        unit.put(RecordKind.ORDER, "order-1", payload, expected_version=0)


def test_vault_references_are_allowed() -> None:
    store = InMemoryPersistence()
    unit = _begin(store, "pii-vault")
    unit.put(
        RecordKind.ORDER,
        "order-1",
        {
            "branch_id": BRANCH,
            "delivery_address_vault_ref": "vault://pii/address/1",
            "recipient_phone_vault_ref": "vault://pii/phone/1",
        },
        expected_version=0,
    )
    unit.commit()
    assert store.get(RecordKind.ORDER, BRANCH, "order-1") is not None


def test_ledger_cannot_commit_without_outbox() -> None:
    store = InMemoryPersistence()
    unit = _begin(store, "ledger-alone")
    unit.put(
        RecordKind.LEDGER_TRANSACTION,
        "ledger-1",
        {"branch_id": BRANCH, "entries": [
            {"account_code": "RIDER_EARNING_EXPENSE", "amount_won": 1},
            {"account_code": "RIDER_PAYABLE", "amount_won": -1},
        ]},
        expected_version=0,
    )
    with pytest.raises(AtomicityViolation):
        unit.commit()
    assert store.get(RecordKind.LEDGER_TRANSACTION, BRANCH, "ledger-1") is None


def test_financial_outbox_cannot_commit_without_ledger() -> None:
    store = InMemoryPersistence()
    unit = _begin(store, "outbox-alone")
    unit.put(
        RecordKind.OUTBOX_MESSAGE,
        "outbox-1",
        {"branch_id": BRANCH, "requires_ledger": True},
        expected_version=0,
    )
    with pytest.raises(AtomicityViolation):
        unit.commit()


def test_failure_in_one_write_rolls_back_entire_bundle() -> None:
    store = InMemoryPersistence()
    seed = _begin(store, "seed")
    seed.put(RecordKind.ORDER, "order-1", {"branch_id": BRANCH}, expected_version=0)
    seed.commit()

    unit = _begin(store, "all-or-nothing")
    unit.put(
        RecordKind.ORDER,
        "order-1",
        {"branch_id": BRANCH, "state": "PICKED_UP"},
        expected_version=0,
    )
    _stage_financial_bundle(unit)
    with pytest.raises(ConcurrencyConflict):
        unit.commit()

    assert store.get(RecordKind.LEDGER_TRANSACTION, BRANCH, "ledger-1") is None
    assert store.get(RecordKind.OUTBOX_MESSAGE, BRANCH, "outbox-1") is None
