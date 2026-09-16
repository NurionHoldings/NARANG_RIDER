from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from narang_rider.outbox_worker import (
    DeliveryEnvelope,
    DeliveryMetric,
    DeliveryPolicy,
    InMemoryOutboxStore,
    PermanentDeliveryError,
    ReliableOutboxWorker,
    RetryableDeliveryError,
    WorkerStopping,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class Clock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class ScriptedTransport:
    def __init__(self, outcomes: dict[str, list[Exception | None]] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[tuple[str, str]] = []

    def send(
        self, *, partner_id: str, payload: dict[str, Any], idempotency_key: str
    ) -> None:
        del payload
        self.calls.append((partner_id, idempotency_key))
        outcomes = self.outcomes.get(partner_id, [])
        if outcomes:
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise outcome


def message(
    message_id: str,
    *,
    partner: str = "partner-a",
    stream: str = "order-1",
    sequence: int = 1,
    branch: str = "sejong",
) -> DeliveryEnvelope:
    payload = {
        "partner_id": partner,
        "stream_id": stream,
        "sequence": sequence,
        "event_type": "RIDER_STATUS",
        "status": "PICKED_UP",
    }
    return DeliveryEnvelope(
        branch,
        message_id,
        partner,
        stream,
        sequence,
        "RIDER_STATUS",
        payload,
        available_at=NOW,
    )


def worker(
    store: InMemoryOutboxStore,
    transport: ScriptedTransport,
    clock: Clock,
    *,
    worker_id: str = "worker-1",
    policy: DeliveryPolicy | None = None,
    metrics: list[DeliveryMetric] | None = None,
) -> ReliableOutboxWorker:
    return ReliableOutboxWorker(
        branch_id="sejong",
        worker_id=worker_id,
        store=store,
        transport=transport,
        clock=clock,
        jitter=lambda: 0.5,
        policy=policy,
        metric_hook=(metrics if metrics is not None else []).append,
    )


def test_branch_partition_bounded_batch_and_concurrent_workers_do_not_duplicate() -> None:
    store = InMemoryOutboxStore(
        tuple(message(f"s-{index}", stream=f"order-{index}") for index in range(4))
        + (message("other", branch="daejeon"),)
    )
    clock = Clock()
    first_transport = ScriptedTransport()
    second_transport = ScriptedTransport()
    policy = DeliveryPolicy(batch_size=2)

    assert worker(store, first_transport, clock, policy=policy).run_once() == 2
    assert worker(
        store, second_transport, clock, worker_id="worker-2", policy=policy
    ).run_once() == 2

    delivered = {key for _, key in first_transport.calls + second_transport.calls}
    assert len(delivered) == 4
    assert all(key.startswith("sejong:") for key in delivered)
    assert store.get("daejeon", "other").delivered_at is None


def test_crash_after_send_before_ack_recovers_with_same_delivery_key() -> None:
    class CrashOnceStore(InMemoryOutboxStore):
        crashed = False

        def ack(self, envelope: DeliveryEnvelope, *, worker_id: str, now: datetime) -> None:
            if not self.crashed:
                self.crashed = True
                raise RuntimeError("simulated crash after partner accepted")
            super().ack(envelope, worker_id=worker_id, now=now)

    store = CrashOnceStore((message("m-1"),))
    clock = Clock()
    transport = ScriptedTransport()
    delivery_worker = worker(store, transport, clock)
    with pytest.raises(RuntimeError, match="simulated crash"):
        delivery_worker.run_once()

    clock.advance(31)
    assert worker(store, transport, clock, worker_id="recovery").run_once() == 1
    assert transport.calls[0][1] == transport.calls[1][1]
    assert store.get("sejong", "m-1").attempts == 2


def test_retry_backoff_is_deterministic_and_ack_only_follows_success() -> None:
    store = InMemoryOutboxStore((message("m-1"),))
    clock = Clock()
    transport = ScriptedTransport(
        {"partner-a": [RetryableDeliveryError("timeout"), None]}
    )
    delivery_worker = worker(store, transport, clock)

    assert delivery_worker.run_once() == 0
    pending = store.get("sejong", "m-1")
    assert pending.delivered_at is None
    assert pending.available_at == NOW + timedelta(seconds=3)
    clock.advance(3)
    assert delivery_worker.run_once() == 1
    assert store.get("sejong", "m-1").delivered_at == clock.value


def test_per_partner_circuit_does_not_block_healthy_partner() -> None:
    store = InMemoryOutboxStore(
        (
            message("a-1", partner="partner-a", stream="a"),
            message("b-1", partner="partner-b", stream="b"),
        )
    )
    clock = Clock()
    transport = ScriptedTransport(
        {"partner-a": [RetryableDeliveryError("down")], "partner-b": [None]}
    )
    policy = DeliveryPolicy(circuit_failure_threshold=1)

    assert worker(store, transport, clock, policy=policy).run_once() == 1
    assert store.get("sejong", "a-1").delivered_at is None
    assert store.get("sejong", "b-1").delivered_at == NOW


def test_ordering_gap_blocks_later_financial_or_rider_status_event() -> None:
    store = InMemoryOutboxStore(
        (message("second", sequence=2), message("first", sequence=1))
    )
    clock = Clock()
    transport = ScriptedTransport()
    policy = DeliveryPolicy(batch_size=1)
    delivery_worker = worker(store, transport, clock, policy=policy)

    assert delivery_worker.run_once() == 1
    assert transport.calls[0][1].endswith(":first")
    assert delivery_worker.run_once() == 1
    assert transport.calls[1][1].endswith(":second")


def test_dead_letter_is_audited_for_human_review_without_financial_adjustment() -> None:
    store = InMemoryOutboxStore((message("m-1"),))
    metrics: list[DeliveryMetric] = []
    transport = ScriptedTransport(
        {"partner-a": [PermanentDeliveryError("invalid contract")]}
    )

    assert worker(store, transport, Clock(), metrics=metrics).run_once() == 0
    dead = store.get("sejong", "m-1")
    assert dead.dead_lettered_at == NOW
    assert dead.delivered_at is None
    assert store.audits[0].review_required is True
    assert store.audits[0].financial_adjustment_allowed is False
    assert metrics[0].name == "human_review_required"
    assert not hasattr(metrics[0], "payload")


def test_retry_exhaustion_dead_letters_and_keeps_sequence_blocked() -> None:
    store = InMemoryOutboxStore(
        (message("first", sequence=1), message("second", sequence=2))
    )
    clock = Clock()
    transport = ScriptedTransport(
        {"partner-a": [RetryableDeliveryError("down")]}
    )
    policy = DeliveryPolicy(max_attempts=1, circuit_failure_threshold=2)
    delivery_worker = worker(store, transport, clock, policy=policy)

    assert delivery_worker.run_once() == 0
    assert store.get("sejong", "first").dead_lettered_at == NOW
    assert delivery_worker.run_once() == 0
    assert store.get("sejong", "second").attempts == 0


def test_graceful_stop_releases_remaining_lease_and_stopped_worker_refuses_poll() -> None:
    store = InMemoryOutboxStore(
        (message("first", stream="one"), message("second", stream="two"))
    )
    clock = Clock()

    class StopAfterFirst(ScriptedTransport):
        target: ReliableOutboxWorker | None = None

        def send(
            self, *, partner_id: str, payload: dict[str, Any], idempotency_key: str
        ) -> None:
            super().send(
                partner_id=partner_id, payload=payload, idempotency_key=idempotency_key
            )
            assert self.target is not None
            self.target.request_stop()

    transport = StopAfterFirst()
    delivery_worker = worker(store, transport, clock)
    transport.target = delivery_worker
    assert delivery_worker.run_once() == 1
    assert store.get("sejong", "second").lease_owner is None
    with pytest.raises(WorkerStopping):
        delivery_worker.run_once()
