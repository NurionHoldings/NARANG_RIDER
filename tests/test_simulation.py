from __future__ import annotations

import json

import pytest

from narang_rider.simulation import (
    MANUAL_NATIONAL_PROFILE,
    SMOKE_PROFILE,
    AbuseDisposition,
    AbuseKind,
    CapacityThresholds,
    DemandClass,
    SimulationProfile,
    generate_scenario,
    run_simulation,
)


def test_seeded_scenario_is_deterministic_synthetic_and_nationally_partitioned() -> None:
    first = generate_scenario(SMOKE_PROFILE)
    second = generate_scenario(SMOKE_PROFILE)
    assert first == second
    assert len(first) == 800
    assert len({item.branch_id for item in first}) == 8
    assert all(item.branch_id.startswith("syn-") for item in first)
    assert all(item.merchant_id.startswith("syn-") for item in first)
    assert {item.demand_class for item in first} == set(DemandClass)
    assert all("@" not in repr(item) and "010-" not in repr(item) for item in first)


def test_smoke_simulation_checks_load_fairness_economics_and_invariants() -> None:
    report = run_simulation()
    assert report.synthetic_only
    assert report.capacity_passed
    assert "not production evidence" in report.disclaimer
    assert report.load.throughput_per_second >= 100
    assert report.load.latency_ms.p50 <= report.load.latency_ms.p95 <= report.load.latency_ms.p99
    assert report.fairness.wait_minutes.p95 <= 18
    assert report.fairness.decline_penalties == 0
    assert report.economics.regional_fund_won_total > 0
    assert report.economics.platform_unit_economics_won_mean == 450
    assert all(item.passed for item in report.invariants)


def test_machine_readable_report_is_stable_and_self_disclaiming() -> None:
    one = run_simulation()
    two = run_simulation()
    assert one.to_json() == two.to_json()
    payload = json.loads(one.to_json())
    assert payload["schema_version"] == "narang.synthetic-simulation.v1"
    assert payload["synthetic_only"] is True
    assert payload["report_digest"] == one.report_digest
    assert len(payload["report_digest"]) == 64


def test_all_required_abuse_scenarios_are_exercised_without_auto_penalty_or_money_move() -> None:
    report = run_simulation()
    assert {item.kind for item in report.abuse} == set(AbuseKind)
    assert all(not item.automatic_penalty for item in report.abuse)
    assert all(not item.money_moved for item in report.abuse)
    dispositions = {item.kind: item.disposition for item in report.abuse}
    assert dispositions[AbuseKind.ORDER_REPLAY] is AbuseDisposition.IDEMPOTENT_REPLAY
    assert dispositions[AbuseKind.CALLBACK_REPLAY] is AbuseDisposition.IDEMPOTENT_REPLAY
    assert dispositions[AbuseKind.LINKED_ACCOUNTS] is AbuseDisposition.PRIORITY_ONLY_REVIEW
    assert dispositions[AbuseKind.GPS_SPOOF] is AbuseDisposition.PRIORITY_ONLY_REVIEW
    assert dispositions[AbuseKind.QUOTE_TAMPER] is AbuseDisposition.BLOCKED
    assert dispositions[AbuseKind.COLLUSIVE_BUNDLE] is AbuseDisposition.BLOCKED
    assert dispositions[AbuseKind.CANCELLATION_REFUND] is AbuseDisposition.BLOCKED
    assert dispositions[AbuseKind.DB_RETRY_DEADLOCK] is AbuseDisposition.RETRIED


def test_flash_demand_partner_outage_and_notification_flood_are_bounded() -> None:
    report = run_simulation()
    assert any(item.kind is AbuseKind.PARTNER_OUTAGE for item in report.abuse)
    assert any(item.kind is AbuseKind.NOTIFICATION_FLOOD for item in report.abuse)
    assert report.load.failure_rate <= CapacityThresholds().max_failure_rate
    assert report.load.max_queue_age_ms <= CapacityThresholds().max_queue_age_ms


def test_capacity_failure_is_explicit_not_hidden() -> None:
    report = run_simulation(thresholds=CapacityThresholds(min_throughput_per_second=1_000_000))
    assert not report.capacity_passed
    assert all(item.passed for item in report.invariants)


def test_profiles_are_bounded_and_manual_scale_is_not_ci_default() -> None:
    assert SMOKE_PROFILE.orders == 800
    assert MANUAL_NATIONAL_PROFILE.orders == 250_000
    assert MANUAL_NATIONAL_PROFILE.name == "manual_national"
    with pytest.raises(ValueError):
        SimulationProfile("too-large", 1, 1, 1, 1, 1_000_001, 1)


def test_no_synthetic_result_can_enable_ai_forbidden_authority() -> None:
    report = run_simulation()
    invariant = next(item for item in report.invariants if item.code == "NO_AI_FORBIDDEN_DECISION")
    assert invariant.passed
    for observation in report.abuse:
        if observation.kind in {AbuseKind.LINKED_ACCOUNTS, AbuseKind.GPS_SPOOF}:
            assert observation.disposition is AbuseDisposition.PRIORITY_ONLY_REVIEW
