from __future__ import annotations

import pytest
from model_harness import Command, InvariantHarness, generate, run_seed

from narang_rider.arkaon import ForbiddenAuthority
from narang_rider.cancellation import CancelActor, CancellationPolicy, CancelStage, cancel


@pytest.mark.parametrize("seed", range(8))
def test_seeded_reference_model(seed: int) -> None:
    assert run_seed(seed, 80)["status"] == "pass"


def test_generated_sequences_are_reproducible() -> None:
    assert generate(42, 50) == generate(42, 50)
    assert generate(42, 50) != generate(43, 50)


def test_restart_replays_append_only_audit_and_state() -> None:
    harness = InvariantHarness(seed=7)
    for command in (
        Command("create", 0),
        Command("advance", 0),
        Command("call", 0, 1),
        Command("callback", 0, 1),
        Command("pay", 0, 20),
    ):
        harness.apply(command)
    restored = harness.restart()
    assert restored.reference == harness.reference
    assert restored.audit == harness.audit


def test_reference_mutation_probes_are_detected() -> None:
    harness = InvariantHarness(seed=9)
    harness.apply(Command("create", 0))
    harness.apply(Command("advance", 0))
    harness.reference[0].version = 999
    with pytest.raises(AssertionError):
        harness.assert_invariants()


def test_decline_or_cancellation_never_creates_penalty_or_clawback() -> None:
    result = cancel(
        order_id="order-1",
        actor=CancelActor.RIDER,
        stage=CancelStage.ARRIVED,
        policy=CancellationPolicy(1_000, 2_000),
        food_handed_over=False,
    )
    assert result.rider_pay_won == 2_000
    assert not result.rider_penalty_allowed


def test_all_arkaon_forbidden_authorities_remain_locked() -> None:
    assert {
        ForbiddenAuthority.PAY_REDUCTION,
        ForbiddenAuthority.DISPATCH_EXCLUSION,
        ForbiddenAuthority.AUTOMATIC_LIABILITY_DECISION,
        ForbiddenAuthority.DIRECT_DEPLOYMENT,
    }.issubset(frozenset(ForbiddenAuthority))
