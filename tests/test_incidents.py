from datetime import UTC, datetime

import pytest

from narang_rider.incidents import (
    ArkaonIncidentRecommendation,
    BranchServiceMode,
    Incident,
    IncidentOperations,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
)
from narang_rider.network import (
    Branch,
    BranchRegistry,
    BranchStatus,
    BranchType,
)

NOW = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)


def operations():
    branches = BranchRegistry()
    branches.register(
        Branch("hq", BranchType.HEADQUARTERS, "본사", None, BranchStatus.ACTIVE, (), 1, NOW)
    )
    branches.register(
        Branch(
            "central",
            BranchType.REGIONAL_BRANCH,
            "중부",
            "hq",
            BranchStatus.ACTIVE,
            (),
            1,
            NOW,
        )
    )
    branches.register(
        Branch(
            "sejong-hub",
            BranchType.LOCAL_HUB,
            "세종",
            "central",
            BranchStatus.ACTIVE,
            (),
            1,
            NOW,
        )
    )
    branches.register(
        Branch(
            "seoul-hub",
            BranchType.LOCAL_HUB,
            "서울",
            "central",
            BranchStatus.ACTIVE,
            (),
            1,
            NOW,
        )
    )
    return IncidentOperations(branches=branches)


def incident():
    return Incident(
        incident_id="incident-1",
        branch_id="sejong-hub",
        incident_type=IncidentType.RIDER_SAFETY,
        severity=IncidentSeverity.CRITICAL,
        summary="extreme weather makes riding unsafe",
        evidence_refs=("vault:weather-alert:1",),
        detected_at=NOW,
        detected_by="safety-monitor",
    )


def test_incident_requires_vault_evidence() -> None:
    with pytest.raises(ValueError, match="VALID_INCIDENT_EVIDENCE_REQUIRED"):
        Incident(
            incident_id="unsafe",
            branch_id="sejong-hub",
            incident_type=IncidentType.PRIVACY,
            severity=IncidentSeverity.HIGH,
            summary="raw document attached",
            evidence_refs=("raw:/customer/address.jpg",),
            detected_at=NOW,
            detected_by="monitor",
        )


def test_arkaon_can_recommend_but_cannot_execute_resolve_or_penalize() -> None:
    value = operations()
    value.open(incident(), event_id="open")
    recommendation = value.recommend(
        ArkaonIncidentRecommendation(
            recommendation_id="recommendation-1",
            incident_id="incident-1",
            suggested_mode=BranchServiceMode.PAUSED,
            rationale="critical wind threshold exceeded",
            confidence_bps=9_500,
            created_at=NOW,
        )
    )

    assert recommendation.may_execute is False
    assert recommendation.may_resolve is False
    assert recommendation.may_penalize_rider is False
    with pytest.raises(ValueError, match="ARKAON_INCIDENT_AUTHORITY_FORBIDDEN"):
        ArkaonIncidentRecommendation(
            recommendation_id="unsafe",
            incident_id="incident-1",
            suggested_mode=BranchServiceMode.PAUSED,
            rationale="unsafe authority",
            confidence_bps=10_000,
            created_at=NOW,
            may_penalize_rider=True,
        )


def test_incident_requires_human_acknowledgement_before_containment() -> None:
    value = operations()
    value.open(incident(), event_id="open")
    with pytest.raises(ValueError, match="ACKNOWLEDGED_INCIDENT_REQUIRED"):
        value.contain(
            incident_id="incident-1",
            event_id="contain",
            actor_id="operator",
            mode=BranchServiceMode.PAUSED,
            now=NOW,
        )
    acknowledged = value.acknowledge(
        incident_id="incident-1",
        event_id="ack",
        actor_id="operator",
        now=NOW,
    )
    assert acknowledged.status is IncidentStatus.ACKNOWLEDGED


def test_containment_is_branch_local_and_preserves_other_regions() -> None:
    value = operations()
    value.open(incident(), event_id="open")
    value.acknowledge(
        incident_id="incident-1",
        event_id="ack",
        actor_id="operator",
        now=NOW,
    )
    contained = value.contain(
        incident_id="incident-1",
        event_id="contain",
        actor_id="safety-manager",
        mode=BranchServiceMode.PAUSED,
        now=NOW,
    )

    assert contained.status is IncidentStatus.CONTAINED
    assert value.branch_mode("sejong-hub") is BranchServiceMode.PAUSED
    assert value.branch_mode("seoul-hub") is BranchServiceMode.NORMAL


def test_resolution_requires_independent_human_and_restores_service() -> None:
    value = operations()
    value.open(incident(), event_id="open")
    value.acknowledge(
        incident_id="incident-1",
        event_id="ack",
        actor_id="operator",
        now=NOW,
    )
    value.contain(
        incident_id="incident-1",
        event_id="contain",
        actor_id="safety-manager",
        mode=BranchServiceMode.SAFE_ONLY,
        now=NOW,
    )
    with pytest.raises(ValueError, match="INDEPENDENT_INCIDENT_CLOSURE_REQUIRED"):
        value.resolve(
            incident_id="incident-1",
            event_id="resolve-self",
            actor_id="safety-manager",
            resolution="weather normalized",
            now=NOW,
        )
    resolved = value.resolve(
        incident_id="incident-1",
        event_id="resolve",
        actor_id="independent-duty-manager",
        resolution="weather normalized and field confirmation received",
        now=NOW,
    )

    assert resolved.status is IncidentStatus.RESOLVED
    assert value.branch_mode("sejong-hub") is BranchServiceMode.NORMAL


def test_incident_timeline_is_append_only_and_event_ids_cannot_replay() -> None:
    value = operations()
    value.open(incident(), event_id="event-1")
    with pytest.raises(ValueError, match="DUPLICATE_INCIDENT_EVENT"):
        value.acknowledge(
            incident_id="incident-1",
            event_id="event-1",
            actor_id="operator",
            now=NOW,
        )
    assert len(value.events("incident-1")) == 1
