from __future__ import annotations

from datetime import UTC, datetime

import pytest

from narang_rider.observability import (
    AlertEvaluator,
    AlertPolicy,
    AlertState,
    DependencyHealth,
    IncidentSnapshot,
    InMemoryRecoveryBackend,
    MetricEvent,
    MetricName,
    MetricRejected,
    PrivacySafeMetricsRegistry,
    ReadinessState,
    RecoveryAction,
    RecoveryCommandService,
    RecoveryRejected,
    RecoveryStatus,
    evaluate_readiness,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_registry_accepts_only_fixed_low_cardinality_dimensions() -> None:
    registry = PrivacySafeMetricsRegistry()
    registry.observe(MetricEvent(MetricName.RETRY, 1, "pilot", "platform", NOW))
    assert registry.values(MetricName.RETRY, "pilot", "platform") == (1.0,)
    with pytest.raises(MetricRejected):
        registry.observe(
            MetricEvent(MetricName.ERROR, 1, "branch-sejong-001", "platform", NOW)
        )
    with pytest.raises(MetricRejected):
        registry.observe(
            MetricEvent(MetricName.ERROR, 1, "pilot", "partner-account-92817", NOW)
        )


def test_alert_hysteresis_prevents_threshold_flapping_storm() -> None:
    evaluator = AlertEvaluator(
        AlertPolicy(MetricName.ERROR, fire_at=0.1, recover_below=0.05, fire_samples=3)
    )
    transitions = [
        transition
        for value in (0.11, 0.09, 0.12, 0.11, 0.15, 0.06, 0.04, 0.06, 0.04, 0.03, 0.02)
        if (transition := evaluator.evaluate(value)) is not None
    ]
    assert [item.current for item in transitions] == [AlertState.FIRING, AlertState.OK]


@pytest.mark.parametrize(
    ("health", "expected"),
    [
        (DependencyHealth(True, True, True), ReadinessState.READY),
        (DependencyHealth(True, False, True), ReadinessState.DEGRADED),
        (DependencyHealth(True, True, False), ReadinessState.DEGRADED),
        (DependencyHealth(False, True, True), ReadinessState.NOT_READY),
    ],
)
def test_dependency_health_degrades_readiness(
    health: DependencyHealth, expected: ReadinessState
) -> None:
    assert evaluate_readiness(health) == expected


def test_incident_snapshot_contains_only_opaque_references() -> None:
    snapshot = IncidentSnapshot.capture(
        branch_class="standard",
        partner_class="agency",
        alert_names=("queue_age",),
        internal_refs=("order-123", "rider-456"),
        captured_at=NOW,
    )
    rendered = repr(snapshot)
    assert "order-123" not in rendered
    assert "rider-456" not in rendered
    assert all(len(item) == 20 for item in snapshot.opaque_refs)


def request(
    service: RecoveryCommandService,
    *,
    command_id: str = "cmd-1",
    action: RecoveryAction = RecoveryAction.PAUSE_PARTNER,
    financial: bool = False,
    branch: str = "sejong",
) -> None:
    service.request(
        command_id=command_id,
        branch_id=branch,
        action=action,
        opaque_target_ref="opaque-abc",
        partner_id="partner-a",
        reason="incident containment",
        ticket_ref="INC-2026-001",
        requested_by="operator-a",
        financial_event=financial,
        now=NOW,
    )


def test_recovery_requires_reason_and_ticket() -> None:
    service = RecoveryCommandService(InMemoryRecoveryBackend())
    with pytest.raises(RecoveryRejected):
        service.request(
            command_id="cmd",
            branch_id="sejong",
            action=RecoveryAction.PAUSE_PARTNER,
            opaque_target_ref="opaque",
            partner_id="partner",
            reason=" ",
            ticket_ref="",
            requested_by="operator",
            financial_event=False,
            now=NOW,
        )


def test_cross_branch_execution_and_self_approval_are_denied() -> None:
    service = RecoveryCommandService(InMemoryRecoveryBackend())
    request(service)
    with pytest.raises(RecoveryRejected):
        service.approve(branch_id="sejong", command_id="cmd-1", approver="operator-a")
    with pytest.raises(RecoveryRejected):
        service.approve(branch_id="daejeon", command_id="cmd-1", approver="operator-b")


def test_financial_recovery_requires_two_independent_approvers() -> None:
    backend = InMemoryRecoveryBackend()
    service = RecoveryCommandService(backend)
    request(service, financial=True)
    first = service.approve(branch_id="sejong", command_id="cmd-1", approver="reviewer-1")
    assert first.status == RecoveryStatus.REQUESTED
    with pytest.raises(RecoveryRejected, match="incomplete"):
        service.execute(branch_id="sejong", command_id="cmd-1", now=NOW)
    second = service.approve(branch_id="sejong", command_id="cmd-1", approver="reviewer-2")
    assert second.status == RecoveryStatus.APPROVED
    assert service.execute(
        branch_id="sejong", command_id="cmd-1", now=NOW
    ).status == RecoveryStatus.EXECUTED


def test_command_request_and_execution_replays_are_idempotent() -> None:
    backend = InMemoryRecoveryBackend()
    service = RecoveryCommandService(backend)
    request(service)
    service.approve(branch_id="sejong", command_id="cmd-1", approver="reviewer")
    first = service.execute(branch_id="sejong", command_id="cmd-1", now=NOW)
    second = service.execute(branch_id="sejong", command_id="cmd-1", now=NOW)
    request(service)
    assert first == second
    assert backend.executions == ["cmd-1"]


def test_command_id_reuse_with_changed_intent_is_rejected() -> None:
    service = RecoveryCommandService(InMemoryRecoveryBackend())
    request(service)
    with pytest.raises(RecoveryRejected, match="different intent"):
        request(service, action=RecoveryAction.RESUME_PARTNER)


def test_dead_letter_recovery_order_requires_partner_pause_first() -> None:
    backend = InMemoryRecoveryBackend()
    service = RecoveryCommandService(backend)
    request(service, action=RecoveryAction.REQUEUE_DEAD_LETTER)
    service.approve(branch_id="sejong", command_id="cmd-1", approver="reviewer")
    with pytest.raises(RecoveryRejected, match="paused"):
        service.execute(branch_id="sejong", command_id="cmd-1", now=NOW)

    request(service, command_id="pause", action=RecoveryAction.PAUSE_PARTNER)
    service.approve(branch_id="sejong", command_id="pause", approver="reviewer")
    service.execute(branch_id="sejong", command_id="pause", now=NOW)
    result = service.execute(branch_id="sejong", command_id="cmd-1", now=NOW)
    assert result.status == RecoveryStatus.EXECUTED
    assert ("sejong", "opaque-abc") in backend.requeued


def test_recovery_audit_is_append_only_and_backend_has_no_ledger_mutator() -> None:
    backend = InMemoryRecoveryBackend()
    service = RecoveryCommandService(backend)
    request(service)
    service.approve(branch_id="sejong", command_id="cmd-1", approver="reviewer")
    service.execute(branch_id="sejong", command_id="cmd-1", now=NOW)
    assert [item.status for item in service.audit_log] == [
        RecoveryStatus.REQUESTED,
        RecoveryStatus.APPROVED,
        RecoveryStatus.EXECUTED,
    ]
    assert not hasattr(backend, "ledger")
    assert not hasattr(backend, "clawback")
