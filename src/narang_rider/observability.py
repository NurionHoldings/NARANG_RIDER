"""Privacy-safe operational telemetry and audited recovery controls."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol


class MetricRejected(ValueError):
    """A metric attempted to expose an identifier or unbounded label."""


class RecoveryRejected(RuntimeError):
    """A recovery command violated authorization or ordering rules."""


class MetricName(StrEnum):
    LATENCY_MS = "latency_ms"
    ERROR = "error"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"
    QUEUE_AGE_SECONDS = "queue_age_seconds"
    LEDGER_CONFLICT = "ledger_conflict"
    AUTH_FAILURE = "auth_failure"


ALLOWED_BRANCH_CLASSES = frozenset({"pilot", "standard", "high_volume"})
ALLOWED_PARTNER_CLASSES = frozenset({"pos", "platform", "agency", "internal"})


@dataclass(frozen=True)
class MetricEvent:
    name: MetricName
    value: float
    branch_class: str
    partner_class: str
    occurred_at: datetime


class PrivacySafeMetricsRegistry:
    """Fixed-cardinality metric aggregation; raw events and labels are not retained."""

    def __init__(self) -> None:
        self._values: dict[tuple[MetricName, str, str], list[float]] = {}
        self._lock = threading.RLock()

    def observe(self, event: MetricEvent) -> None:
        if event.branch_class not in ALLOWED_BRANCH_CLASSES:
            raise MetricRejected("unknown branch class")
        if event.partner_class not in ALLOWED_PARTNER_CLASSES:
            raise MetricRejected("unknown partner class")
        if not 0 <= event.value < 1_000_000_000:
            raise MetricRejected("metric value outside safe bounds")
        key = (event.name, event.branch_class, event.partner_class)
        with self._lock:
            self._values.setdefault(key, []).append(float(event.value))

    def values(
        self, name: MetricName, branch_class: str, partner_class: str
    ) -> tuple[float, ...]:
        with self._lock:
            return tuple(self._values.get((name, branch_class, partner_class), ()))


class AlertState(StrEnum):
    OK = "ok"
    FIRING = "firing"


@dataclass(frozen=True)
class AlertPolicy:
    metric: MetricName
    fire_at: float
    recover_below: float
    fire_samples: int = 3
    recover_samples: int = 3

    def __post_init__(self) -> None:
        if self.fire_at <= self.recover_below:
            raise ValueError("alert recovery threshold must be lower than fire threshold")
        if self.fire_samples < 1 or self.recover_samples < 1:
            raise ValueError("alert sample counts must be positive")


@dataclass(frozen=True)
class AlertTransition:
    previous: AlertState
    current: AlertState
    metric: MetricName
    value: float


class AlertEvaluator:
    """Consecutive-sample hysteresis prevents alert/recovery storms."""

    def __init__(self, policy: AlertPolicy) -> None:
        self.policy = policy
        self.state = AlertState.OK
        self._high = 0
        self._low = 0

    def evaluate(self, value: float) -> AlertTransition | None:
        previous = self.state
        if self.state == AlertState.OK:
            self._high = self._high + 1 if value >= self.policy.fire_at else 0
            if self._high >= self.policy.fire_samples:
                self.state = AlertState.FIRING
                self._high = 0
        else:
            self._low = self._low + 1 if value < self.policy.recover_below else 0
            if self._low >= self.policy.recover_samples:
                self.state = AlertState.OK
                self._low = 0
        if previous == self.state:
            return None
        return AlertTransition(previous, self.state, self.policy.metric, value)


class ReadinessState(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    NOT_READY = "not_ready"


@dataclass(frozen=True)
class DependencyHealth:
    database: bool
    outbox: bool
    jwks: bool


def evaluate_readiness(health: DependencyHealth) -> ReadinessState:
    if not health.database:
        return ReadinessState.NOT_READY
    if not health.outbox or not health.jwks:
        return ReadinessState.DEGRADED
    return ReadinessState.READY


@dataclass(frozen=True)
class IncidentSnapshot:
    snapshot_id: str
    branch_class: str
    partner_class: str
    alert_names: tuple[str, ...]
    opaque_refs: tuple[str, ...]
    captured_at: datetime

    @classmethod
    def capture(
        cls,
        *,
        branch_class: str,
        partner_class: str,
        alert_names: tuple[str, ...],
        internal_refs: tuple[str, ...],
        captured_at: datetime,
    ) -> IncidentSnapshot:
        if branch_class not in ALLOWED_BRANCH_CLASSES:
            raise MetricRejected("unknown branch class")
        if partner_class not in ALLOWED_PARTNER_CLASSES:
            raise MetricRejected("unknown partner class")
        opaque = tuple(
            hashlib.sha256(f"incident:{item}".encode()).hexdigest()[:20]
            for item in internal_refs
        )
        material = "|".join((branch_class, partner_class, *alert_names, *opaque))
        return cls(
            hashlib.sha256(material.encode()).hexdigest()[:24],
            branch_class,
            partner_class,
            alert_names,
            opaque,
            captured_at,
        )


class RecoveryAction(StrEnum):
    PAUSE_PARTNER = "pause_partner"
    RELEASE_STALE_LEASE = "release_stale_lease"
    REQUEUE_DEAD_LETTER = "requeue_dead_letter"
    RESUME_PARTNER = "resume_partner"


class RecoveryStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    EXECUTED = "executed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class RecoveryCommand:
    command_id: str
    branch_id: str
    action: RecoveryAction
    opaque_target_ref: str
    partner_id: str
    reason: str
    ticket_ref: str
    requested_by: str
    financial_event: bool
    approvals: tuple[str, ...] = ()
    status: RecoveryStatus = RecoveryStatus.REQUESTED
    requested_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))
    executed_at: datetime | None = None


class RecoveryBackend(Protocol):
    def execute(self, command: RecoveryCommand) -> None: ...


class InMemoryRecoveryBackend:
    """Models control changes only; it has no ledger mutation capability."""

    def __init__(self) -> None:
        self.paused: set[tuple[str, str]] = set()
        self.requeued: set[tuple[str, str]] = set()
        self.released: set[tuple[str, str]] = set()
        self.executions: list[str] = []

    def execute(self, command: RecoveryCommand) -> None:
        key = (command.branch_id, command.opaque_target_ref)
        if command.action == RecoveryAction.PAUSE_PARTNER:
            self.paused.add((command.branch_id, command.partner_id))
        elif command.action == RecoveryAction.RESUME_PARTNER:
            self.paused.discard((command.branch_id, command.partner_id))
        elif command.action == RecoveryAction.REQUEUE_DEAD_LETTER:
            self.requeued.add(key)
        elif command.action == RecoveryAction.RELEASE_STALE_LEASE:
            self.released.add(key)
        self.executions.append(command.command_id)


class RecoveryCommandService:
    """Append-only, idempotent recovery state machine with approval gates."""

    def __init__(self, backend: RecoveryBackend) -> None:
        self._backend = backend
        self._commands: dict[tuple[str, str], RecoveryCommand] = {}
        self._audit: list[RecoveryCommand] = []
        self._lock = threading.RLock()

    @property
    def audit_log(self) -> tuple[RecoveryCommand, ...]:
        with self._lock:
            return tuple(self._audit)

    def request(
        self,
        *,
        command_id: str,
        branch_id: str,
        action: RecoveryAction,
        opaque_target_ref: str,
        partner_id: str,
        reason: str,
        ticket_ref: str,
        requested_by: str,
        financial_event: bool,
        now: datetime,
    ) -> RecoveryCommand:
        fields = (
            command_id,
            branch_id,
            opaque_target_ref,
            partner_id,
            reason.strip(),
            ticket_ref.strip(),
            requested_by,
        )
        if any(not value for value in fields):
            raise RecoveryRejected("reason, ticket and command scope are required")
        key = (branch_id, command_id)
        command = RecoveryCommand(
            command_id,
            branch_id,
            action,
            opaque_target_ref,
            partner_id,
            reason.strip(),
            ticket_ref.strip(),
            requested_by,
            financial_event,
            requested_at=now,
        )
        with self._lock:
            previous = self._commands.get(key)
            if previous is not None:
                intent = (
                    previous.action,
                    previous.opaque_target_ref,
                    previous.partner_id,
                    previous.reason,
                    previous.ticket_ref,
                    previous.requested_by,
                    previous.financial_event,
                )
                requested_intent = (
                    command.action,
                    command.opaque_target_ref,
                    command.partner_id,
                    command.reason,
                    command.ticket_ref,
                    command.requested_by,
                    command.financial_event,
                )
                if intent != requested_intent:
                    raise RecoveryRejected("command id was reused with different intent")
                return previous
            self._commands[key] = command
            self._audit.append(command)
            return command

    def approve(
        self, *, branch_id: str, command_id: str, approver: str
    ) -> RecoveryCommand:
        key = (branch_id, command_id)
        with self._lock:
            command = self._commands.get(key)
            if command is None:
                raise RecoveryRejected("branch-scoped command not found")
            if approver == command.requested_by:
                raise RecoveryRejected("requester cannot approve their own recovery")
            approvals = tuple(dict.fromkeys((*command.approvals, approver)))
            required = 2 if command.financial_event else 1
            status = RecoveryStatus.APPROVED if len(approvals) >= required else command.status
            updated = replace(command, approvals=approvals, status=status)
            self._commands[key] = updated
            self._audit.append(updated)
            return updated

    def execute(
        self, *, branch_id: str, command_id: str, now: datetime
    ) -> RecoveryCommand:
        key = (branch_id, command_id)
        with self._lock:
            command = self._commands.get(key)
            if command is None:
                raise RecoveryRejected("branch-scoped command not found")
            if command.status == RecoveryStatus.EXECUTED:
                return command
            if command.status != RecoveryStatus.APPROVED:
                raise RecoveryRejected("required approvals are incomplete")
            if command.action == RecoveryAction.REQUEUE_DEAD_LETTER:
                pause = (branch_id, command.partner_id)
                backend = self._backend
                if isinstance(backend, InMemoryRecoveryBackend) and pause not in backend.paused:
                    raise RecoveryRejected("partner must be paused before dead-letter requeue")
            self._backend.execute(command)
            executed = replace(command, status=RecoveryStatus.EXECUTED, executed_at=now)
            self._commands[key] = executed
            self._audit.append(executed)
            return executed


def stale_before(now: datetime, lease_seconds: int) -> datetime:
    if lease_seconds < 1:
        raise ValueError("lease duration must be positive")
    return now - timedelta(seconds=lease_seconds)
