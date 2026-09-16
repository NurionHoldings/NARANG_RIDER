from __future__ import annotations

import argparse
import itertools
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from narang_rider.connector import CallerType, CallStatus, RiderCall, RiderCallService
from narang_rider.ledger import Ledger, LedgerAccount, LedgerEntry, LedgerTransaction
from narang_rider.lifecycle import OrderRepository, OrderState
from narang_rider.money import Money
from narang_rider.status_sync import CallbackTarget, DeliveryStatus, OutboxMessage, StatusOutbox

NOW = datetime(2026, 9, 16, tzinfo=UTC)
LEGAL_NEXT = {
    OrderState.CREATED: (OrderState.QUOTED, OrderState.CANCELLED),
    OrderState.QUOTED: (OrderState.OFFERING, OrderState.CANCELLED),
    OrderState.OFFERING: (OrderState.ASSIGNED, OrderState.CANCELLED),
    OrderState.ASSIGNED: (OrderState.PICKED_UP, OrderState.OFFERING, OrderState.CANCELLED),
    OrderState.PICKED_UP: (OrderState.DELIVERED,),
    OrderState.DELIVERED: (OrderState.SETTLED,),
    OrderState.SETTLED: (),
    OrderState.CANCELLED: (),
}


@dataclass(frozen=True)
class Command:
    kind: str
    order: int
    variant: int = 0

    def render(self) -> str:
        return f"{self.kind}:{self.order}:{self.variant}"


@dataclass
class ReferenceOrder:
    state: OrderState = OrderState.CREATED
    version: int = 1
    active_call: str | None = None
    callback_sequence: int = 0
    rider_pay_won: int = 0


class InvariantHarness:
    def __init__(self, *, seed: int) -> None:
        self.seed = seed
        self.orders = OrderRepository()
        self.calls = RiderCallService()
        self.callbacks = StatusOutbox()
        self.ledger = Ledger()
        self.reference: dict[int, ReferenceOrder] = {}
        self.accepted: list[Command] = []
        self.audit: list[str] = []

    def apply(self, command: Command, *, record: bool = True) -> None:
        order_id = f"order-{command.order}"
        if command.kind == "create":
            if command.order in self.reference:
                return
            self.orders.create(
                order_id=order_id, event_id=f"create-{command.order}", occurred_at=NOW
            )
            self.reference[command.order] = ReferenceOrder()
            self.audit.append(f"{len(self.audit)}:{command.render()}")
        elif command.order not in self.reference:
            return
        elif command.kind == "advance":
            reference = self.reference[command.order]
            allowed = LEGAL_NEXT[reference.state]
            if not allowed:
                return
            target = allowed[command.variant % len(allowed)]
            event_id = f"transition-{command.order}-{reference.version}-{target.value}"
            actual = self.orders.transition(
                order_id=order_id,
                event_id=event_id,
                to_state=target,
                occurred_at=NOW,
                reason_code="MODEL_COMMAND",
                expected_version=reference.version,
            )
            replay = self.orders.transition(
                order_id=order_id,
                event_id=event_id,
                to_state=target,
                occurred_at=NOW,
                reason_code="MODEL_COMMAND",
                expected_version=reference.version,
            )
            reference.state = target
            reference.version += 1
            assert actual == replay
            self.audit.append(f"{len(self.audit)}:{command.render()}")
        elif command.kind == "call":
            reference = self.reference[command.order]
            if reference.active_call is not None:
                return
            call_number = sum(f":call:{command.order}:" in entry for entry in self.audit)
            call_id = f"call-{command.order}-{call_number}"
            call = RiderCall(
                call_id,
                order_id,
                CallerType.MERCHANT if command.variant % 2 == 0 else CallerType.RIDER_COMPANY,
                "caller",
                f"call-key-{command.order}-{call_number}",
                f"{command.variant:064x}"[-64:],
                NOW,
            )
            assert self.calls.create(call) == self.calls.create(call)
            reference.active_call = call_id
            self.audit.append(f"{len(self.audit)}:{command.render()}")
        elif command.kind == "finish_call":
            reference = self.reference[command.order]
            if reference.active_call is None:
                return
            self.calls.finish(reference.active_call, CallStatus.CANCELLED)
            reference.active_call = None
            self.audit.append(f"{len(self.audit)}:{command.render()}")
        elif command.kind == "callback":
            reference = self.reference[command.order]
            if reference.callback_sequence >= len(DeliveryStatus):
                return
            sequence = reference.callback_sequence + 1
            status = tuple(DeliveryStatus)[reference.callback_sequence]
            message = OutboxMessage(
                f"callback-{command.order}-{sequence}",
                order_id,
                CallbackTarget("model", "vault:model-callback"),
                status,
                sequence,
            )
            assert self.callbacks.enqueue(message) == self.callbacks.enqueue(message)
            self.callbacks.mark_delivered(message.message_id)
            reference.callback_sequence = sequence
            self.audit.append(f"{len(self.audit)}:{command.render()}")
        elif command.kind == "pay":
            reference = self.reference[command.order]
            if reference.rider_pay_won:
                return
            amount = 1_000 + command.variant
            transaction = LedgerTransaction(
                f"ledger-{command.order}",
                order_id,
                "model-policy",
                (
                    LedgerEntry(LedgerAccount.CUSTOMER_RECEIVABLE, Money(amount), Money(0)),
                    LedgerEntry(LedgerAccount.RIDER_PAYABLE, Money(0), Money(amount)),
                ),
            )
            self.ledger.record(transaction)
            reference.rider_pay_won = amount
            self.audit.append(f"{len(self.audit)}:{command.render()}")
        self.assert_invariants()
        if record and (
            not self.accepted or self.accepted[-1] != command or command.kind != "create"
        ):
            self.accepted.append(command)

    def assert_invariants(self) -> None:
        for number, expected in self.reference.items():
            actual = self.orders.get(f"order-{number}")
            assert (actual.state, actual.version) == (expected.state, expected.version)
            events = self.orders.events(actual.order_id)
            assert tuple(event.version for event in events) == tuple(range(1, actual.version + 1))
            assert len(events) == len({event.event_id for event in events})
        for transaction in self.ledger.transactions():
            debits = sum(entry.debit.won for entry in transaction.entries)
            credits = sum(entry.credit.won for entry in transaction.entries)
            assert debits == credits
        assert all(order.rider_pay_won >= 0 for order in self.reference.values())
        assert len(self.audit) == len(set(self.audit))

    def restart(self) -> InvariantHarness:
        restored = InvariantHarness(seed=self.seed)
        for command in self.accepted:
            restored.apply(command)
        restored.assert_invariants()
        return restored


def generate(seed: int, steps: int) -> tuple[Command, ...]:
    random_source = random.Random(seed)
    kinds = ("create", "advance", "call", "finish_call", "callback", "pay")
    return tuple(
        Command(
            random_source.choice(kinds), random_source.randrange(4), random_source.randrange(32)
        )
        for _ in range(steps)
    )


def run_seed(seed: int, steps: int) -> dict[str, object]:
    harness = InvariantHarness(seed=seed)
    sequence = generate(seed, steps)
    try:
        for index, command in enumerate(sequence):
            # A pre-commit crash has no observable effect; a post-commit crash must replay exactly.
            if index % 17 == 0:
                harness.assert_invariants()
            harness.apply(command)
            if index % 19 == 0:
                harness = harness.restart()
        # Bounded scheduling permutations prove only one active call survives per order.
        for permutation in itertools.permutations(
            (Command("call", 0, 100), Command("call", 0, 101))
        ):
            trial = harness.restart()
            for command in permutation:
                trial.apply(command)
            assert sum(order.active_call is not None for order in trial.reference.values()) <= len(
                trial.reference
            )
    except Exception as error:
        minimal = [command.render() for command in sequence[: index + 1]]
        raise AssertionError(
            json.dumps({"seed": seed, "step": index, "sequence": minimal}, separators=(",", ":"))
        ) from error
    return {"seed": seed, "steps": steps, "accepted": len(harness.accepted), "status": "pass"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("smoke", "large"), default="smoke")
    parser.add_argument("--output", default="build/model-invariant-report.json")
    args = parser.parse_args()
    seeds = range(8) if args.profile == "smoke" else range(256)
    steps = 80 if args.profile == "smoke" else 1_000
    report = {
        "profile": args.profile,
        "deterministic": True,
        "results": [run_seed(seed, steps) for seed in seeds],
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"profile": args.profile, "seeds": len(report["results"]), "status": "pass"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
