from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import IntEnum, StrEnum

from .network import BranchRegistry


class IncidentType(StrEnum):
    RIDER_SAFETY = "RIDER_SAFETY"
    PRIVACY = "PRIVACY"
    PAYMENT = "PAYMENT"
    MODEL_DRIFT = "MODEL_DRIFT"
    SERVICE_OUTAGE = "SERVICE_OUTAGE"
    EVIDENCE_INTEGRITY = "EVIDENCE_INTEGRITY"


class IncidentSeverity(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    CONTAINED = "CONTAINED"
    RESOLVED = "RESOLVED"


class BranchServiceMode(StrEnum):
    NORMAL = "NORMAL"
    SAFE_ONLY = "SAFE_ONLY"
    PAUSED = "PAUSED"


@dataclass(frozen=True)
class Incident:
    incident_id: str
    branch_id: str
    incident_type: IncidentType
    severity: IncidentSeverity
    summary: str
    evidence_refs: tuple[str, ...]
    detected_at: datetime
    detected_by: str
    status: IncidentStatus = IncidentStatus.OPEN
    acknowledged_by: str | None = None
    contained_by: str | None = None
    resolved_by: str | None = None
    resolution: str | None = None

    def __post_init__(self) -> None:
        if (
            not all(
                value.strip()
                for value in (
                    self.incident_id,
                    self.branch_id,
                    self.summary,
                    self.detected_by,
                )
            )
            or self.detected_at.tzinfo is None
            or not self.evidence_refs
            or any(not ref.startswith("vault:") for ref in self.evidence_refs)
        ):
            raise ValueError("VALID_INCIDENT_EVIDENCE_REQUIRED")


@dataclass(frozen=True)
class IncidentEvent:
    event_id: str
    incident_id: str
    from_status: IncidentStatus | None
    to_status: IncidentStatus
    actor_id: str
    occurred_at: datetime
    reason: str


@dataclass(frozen=True)
class ArkaonIncidentRecommendation:
    recommendation_id: str
    incident_id: str
    suggested_mode: BranchServiceMode
    rationale: str
    confidence_bps: int
    created_at: datetime
    may_execute: bool = False
    may_resolve: bool = False
    may_penalize_rider: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.confidence_bps <= 10_000:
            raise ValueError("INVALID_RECOMMENDATION_CONFIDENCE")
        if self.may_execute or self.may_resolve or self.may_penalize_rider:
            raise ValueError("ARKAON_INCIDENT_AUTHORITY_FORBIDDEN")


class IncidentOperations:
    def __init__(self, *, branches: BranchRegistry) -> None:
        self._branches = branches
        self._incidents: dict[str, Incident] = {}
        self._events: dict[str, IncidentEvent] = {}
        self._branch_modes: dict[str, BranchServiceMode] = {}
        self._recommendations: dict[str, ArkaonIncidentRecommendation] = {}

    def open(self, incident: Incident, *, event_id: str) -> Incident:
        self._branches.get(incident.branch_id)
        if incident.incident_id in self._incidents:
            raise ValueError("DUPLICATE_INCIDENT")
        self._append_event(
            IncidentEvent(
                event_id,
                incident.incident_id,
                None,
                IncidentStatus.OPEN,
                incident.detected_by,
                incident.detected_at,
                "INCIDENT_DETECTED",
            )
        )
        self._incidents[incident.incident_id] = incident
        self._branch_modes.setdefault(incident.branch_id, BranchServiceMode.NORMAL)
        return incident

    def recommend(self, value: ArkaonIncidentRecommendation) -> ArkaonIncidentRecommendation:
        self.get(value.incident_id)
        if value.recommendation_id in self._recommendations:
            existing = self._recommendations[value.recommendation_id]
            if existing != value:
                raise ValueError("RECOMMENDATION_IDEMPOTENCY_CONFLICT")
            return existing
        self._recommendations[value.recommendation_id] = value
        return value

    def acknowledge(
        self,
        *,
        incident_id: str,
        event_id: str,
        actor_id: str,
        now: datetime,
    ) -> Incident:
        incident = self.get(incident_id)
        if incident.status is not IncidentStatus.OPEN:
            raise ValueError("OPEN_INCIDENT_REQUIRED")
        updated = replace(
            incident,
            status=IncidentStatus.ACKNOWLEDGED,
            acknowledged_by=actor_id,
        )
        self._transition(incident, updated, event_id, actor_id, now, "HUMAN_ACKNOWLEDGED")
        return updated

    def contain(
        self,
        *,
        incident_id: str,
        event_id: str,
        actor_id: str,
        mode: BranchServiceMode,
        now: datetime,
    ) -> Incident:
        incident = self.get(incident_id)
        if incident.status is not IncidentStatus.ACKNOWLEDGED:
            raise ValueError("ACKNOWLEDGED_INCIDENT_REQUIRED")
        if mode is BranchServiceMode.NORMAL:
            raise ValueError("CONTAINMENT_MODE_REQUIRED")
        updated = replace(
            incident,
            status=IncidentStatus.CONTAINED,
            contained_by=actor_id,
        )
        self._branch_modes[incident.branch_id] = mode
        self._transition(incident, updated, event_id, actor_id, now, f"MODE:{mode.value}")
        return updated

    def resolve(
        self,
        *,
        incident_id: str,
        event_id: str,
        actor_id: str,
        resolution: str,
        now: datetime,
    ) -> Incident:
        incident = self.get(incident_id)
        if incident.status is not IncidentStatus.CONTAINED:
            raise ValueError("CONTAINED_INCIDENT_REQUIRED")
        if not resolution.strip():
            raise ValueError("RESOLUTION_REQUIRED")
        if actor_id in {incident.detected_by, incident.contained_by}:
            raise ValueError("INDEPENDENT_INCIDENT_CLOSURE_REQUIRED")
        updated = replace(
            incident,
            status=IncidentStatus.RESOLVED,
            resolved_by=actor_id,
            resolution=resolution,
        )
        self._branch_modes[incident.branch_id] = BranchServiceMode.NORMAL
        self._transition(incident, updated, event_id, actor_id, now, resolution)
        return updated

    def branch_mode(self, branch_id: str) -> BranchServiceMode:
        self._branches.get(branch_id)
        return self._branch_modes.get(branch_id, BranchServiceMode.NORMAL)

    def get(self, incident_id: str) -> Incident:
        try:
            return self._incidents[incident_id]
        except KeyError as exc:
            raise ValueError("INCIDENT_NOT_FOUND") from exc

    def events(self, incident_id: str) -> tuple[IncidentEvent, ...]:
        return tuple(event for event in self._events.values() if event.incident_id == incident_id)

    def _transition(
        self,
        before: Incident,
        after: Incident,
        event_id: str,
        actor_id: str,
        now: datetime,
        reason: str,
    ) -> None:
        self._append_event(
            IncidentEvent(
                event_id,
                before.incident_id,
                before.status,
                after.status,
                actor_id,
                now,
                reason,
            )
        )
        self._incidents[before.incident_id] = after

    def _append_event(self, event: IncidentEvent) -> None:
        if event.event_id in self._events:
            raise ValueError("DUPLICATE_INCIDENT_EVENT")
        self._events[event.event_id] = event
