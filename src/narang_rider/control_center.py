"""Privacy-safe national branch control-center application contracts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class ControlError(StrEnum):
    ACCESS_DENIED = "ACCESS_DENIED"
    READINESS_INCOMPLETE = "READINESS_INCOMPLETE"
    SMALL_COHORT_FORBIDDEN = "SMALL_COHORT_FORBIDDEN"
    POLICY_WEAKENING_FORBIDDEN = "POLICY_WEAKENING_FORBIDDEN"
    STALE_VERSION = "STALE_VERSION"
    DUAL_APPROVAL_REQUIRED = "DUAL_APPROVAL_REQUIRED"
    OVERBROAD_COMMAND = "OVERBROAD_COMMAND"
    CORRIDOR_APPROVAL_REQUIRED = "CORRIDOR_APPROVAL_REQUIRED"
    RAW_PII_FORBIDDEN = "RAW_PII_FORBIDDEN"
    AI_AUTHORITY_FORBIDDEN = "AI_AUTHORITY_FORBIDDEN"


class ControlRejected(RuntimeError):
    def __init__(self, code: ControlError) -> None:
        self.code = code
        super().__init__(code.value)


class BranchLevel(StrEnum):
    HQ = "HQ"
    REGIONAL = "REGIONAL"
    LOCAL = "LOCAL"


class Impact(StrEnum):
    LOCAL = "LOCAL"
    NATIONAL = "NATIONAL"
    FINANCIAL = "FINANCIAL"
    PRIVACY = "PRIVACY"


class CommandTarget(StrEnum):
    INTAKE = "INTAKE"
    DISPATCH = "DISPATCH"
    OUTBOX = "OUTBOX"


@dataclass(frozen=True)
class BranchNode:
    branch_id: str
    level: BranchLevel
    parent_id: str | None
    service_zone_ids: frozenset[str]
    active: bool = False

    def __post_init__(self) -> None:
        if not self.branch_id or any(" " in zone or not zone for zone in self.service_zone_ids):
            raise ValueError("COARSE_ZONE_IDS_REQUIRED")


@dataclass(frozen=True)
class ReadinessChecklist:
    branch_id: str
    legal: bool
    security: bool
    privacy: bool
    settlement: bool
    incident: bool
    partner: bool

    @property
    def complete(self) -> bool:
        return all((self.legal, self.security, self.privacy, self.settlement,
                    self.incident, self.partner))


@dataclass(frozen=True)
class NationalPolicy:
    minimum_rider_pay_won: int
    minimum_cohort: int
    settlement_days_max: int
    evidence_retention_days_max: int
    ai_forbidden_powers: frozenset[str]


@dataclass(frozen=True)
class EffectivePolicy:
    policy_id: str
    branch_id: str
    version: int
    effective_at: datetime
    minimum_rider_pay_won: int
    minimum_cohort: int
    settlement_days_max: int
    evidence_retention_days_max: int
    ai_forbidden_powers: frozenset[str]
    replaced_by: str | None = None


@dataclass(frozen=True)
class AggregateSnapshot:
    branch_id: str
    cohort_size: int
    merchant_count: int
    rider_count: int
    order_count: int
    incident_open: int
    partner_circuit_open: int
    queue_depth: int
    settlement_pending_won: int


@dataclass(frozen=True)
class Corridor:
    corridor_id: str
    from_branch_id: str
    to_branch_id: str
    zone_ids: frozenset[str]
    approvals: frozenset[str] = frozenset()


@dataclass(frozen=True)
class OperatorCommand:
    command_id: str
    branch_id: str
    target: CommandTarget
    impact: Impact
    reason: str
    ticket_ref: str
    requested_by: str
    expected_branch_version: int
    created_at: datetime
    expires_at: datetime | None
    approvals: tuple[str, ...] = ()
    executed: bool = False


@dataclass(frozen=True)
class ControlAudit:
    sequence: int
    branch_id: str
    action: str
    subject_id: str
    actor_id: str


CONTROL_CENTER_ROUTES = (
    ("GET", "/api/v1/control/branches", "BranchTreeV1"),
    ("POST", "/api/v1/control/branches/{branch_id}/activate", "ReadinessActivationV1"),
    ("GET", "/api/v1/control/branches/{branch_id}/dashboard", "AggregateDashboardV1"),
    ("POST", "/api/v1/control/branches/{branch_id}/policies", "PolicyOverrideV1"),
    ("POST", "/api/v1/control/commands", "OperatorCommandV1"),
    ("POST", "/api/v1/control/commands/{command_id}/approvals", "CommandApprovalV1"),
    ("POST", "/api/v1/control/corridors/{corridor_id}/transfers", "CorridorTransferV1"),
)


class NationalControlCenter:
    def __init__(self, *, national: NationalPolicy) -> None:
        self.national = national
        self.branches: dict[str, BranchNode] = {}
        self._versions: dict[str, int] = {}
        self._policies: dict[str, list[EffectivePolicy]] = {}
        self._commands: dict[str, OperatorCommand] = {}
        self._corridors: dict[str, Corridor] = {}
        self.audit: list[ControlAudit] = []
        self.outbox: list[ControlAudit] = []

    def register_branch(self, node: BranchNode) -> None:
        if node.parent_id:
            parent = self.branches.get(node.parent_id)
            if parent is None or (node.level is BranchLevel.LOCAL and parent.level is not BranchLevel.REGIONAL):
                raise ControlRejected(ControlError.ACCESS_DENIED)
        self.branches[node.branch_id] = node
        self._versions[node.branch_id] = 1

    def activate(self, checklist: ReadinessChecklist, *, actor_id: str) -> BranchNode:
        node = self._branch(checklist.branch_id)
        if not checklist.complete:
            raise ControlRejected(ControlError.READINESS_INCOMPLETE)
        active = replace(node, active=True)
        self.branches[node.branch_id] = active
        self._versions[node.branch_id] += 1
        self._emit(node.branch_id, "BRANCH_ACTIVATED", node.branch_id, actor_id)
        return active

    def authorize(self, *, actor_branch_id: str, target_branch_id: str) -> None:
        if actor_branch_id == target_branch_id:
            return
        actor = self._branch(actor_branch_id)
        target = self._branch(target_branch_id)
        if actor.level is BranchLevel.HQ:
            return
        if actor.level is BranchLevel.REGIONAL and target.parent_id == actor.branch_id:
            return
        raise ControlRejected(ControlError.ACCESS_DENIED)

    def dashboard(self, *, actor_branch_id: str, snapshot: AggregateSnapshot) -> AggregateSnapshot:
        self.authorize(actor_branch_id=actor_branch_id, target_branch_id=snapshot.branch_id)
        policy = self.policy_at(snapshot.branch_id, datetime.max.replace(tzinfo=UTC))
        if snapshot.cohort_size < policy.minimum_cohort:
            raise ControlRejected(ControlError.SMALL_COHORT_FORBIDDEN)
        return snapshot

    def set_policy(self, *, value: EffectivePolicy, expected_version: int, actor_id: str) -> EffectivePolicy:
        self._branch(value.branch_id)
        if expected_version != self._versions[value.branch_id] or value.version != expected_version + 1:
            raise ControlRejected(ControlError.STALE_VERSION)
        if (value.minimum_rider_pay_won < self.national.minimum_rider_pay_won
                or value.minimum_cohort < self.national.minimum_cohort
                or value.settlement_days_max > self.national.settlement_days_max
                or value.evidence_retention_days_max > self.national.evidence_retention_days_max
                or not self.national.ai_forbidden_powers <= value.ai_forbidden_powers):
            raise ControlRejected(ControlError.POLICY_WEAKENING_FORBIDDEN)
        self._policies.setdefault(value.branch_id, []).append(value)
        self._versions[value.branch_id] = value.version
        self._emit(value.branch_id, "POLICY_VERSIONED", value.policy_id, actor_id)
        return value

    def policy_at(self, branch_id: str, at: datetime) -> EffectivePolicy:
        values = [p for p in self._policies.get(branch_id, ()) if p.effective_at <= at]
        if values:
            return max(values, key=lambda p: (p.effective_at, p.version))
        return EffectivePolicy("national", branch_id, self._versions.get(branch_id, 1), at,
            self.national.minimum_rider_pay_won, self.national.minimum_cohort,
            self.national.settlement_days_max, self.national.evidence_retention_days_max,
            self.national.ai_forbidden_powers)

    def request_command(self, command: OperatorCommand) -> OperatorCommand:
        self._branch(command.branch_id)
        if (not command.reason.strip() or not command.ticket_ref.strip()
                or command.expected_branch_version != self._versions[command.branch_id]):
            raise ControlRejected(ControlError.STALE_VERSION)
        if command.impact is Impact.LOCAL and self.branches[command.branch_id].level is BranchLevel.HQ:
            raise ControlRejected(ControlError.OVERBROAD_COMMAND)
        if command.expires_at and command.expires_at > command.created_at + timedelta(hours=4):
            raise ControlRejected(ControlError.OVERBROAD_COMMAND)
        existing = self._commands.get(command.command_id)
        if existing and existing != command:
            raise ControlRejected(ControlError.STALE_VERSION)
        if not existing:
            self._commands[command.command_id] = command
            self._emit(command.branch_id, "COMMAND_REQUESTED", command.command_id, command.requested_by)
        return existing or command

    def approve_command(self, command_id: str, *, approver_id: str) -> OperatorCommand:
        value = self._commands[command_id]
        if approver_id == value.requested_by:
            raise ControlRejected(ControlError.DUAL_APPROVAL_REQUIRED)
        approvals = value.approvals if approver_id in value.approvals else value.approvals + (approver_id,)
        required = 2 if value.impact in {Impact.NATIONAL, Impact.FINANCIAL, Impact.PRIVACY} else 1
        updated = replace(value, approvals=approvals, executed=len(approvals) >= required)
        self._commands[command_id] = updated
        self._emit(value.branch_id, "COMMAND_APPROVED", command_id, approver_id)
        return updated

    def expire_emergency(self, *, now: datetime) -> tuple[str, ...]:
        expired = []
        for key, value in tuple(self._commands.items()):
            if value.executed and value.expires_at and value.expires_at <= now:
                self._commands[key] = replace(value, executed=False)
                expired.append(key)
                self._emit(value.branch_id, "EMERGENCY_EXPIRED_REVIEW_REQUIRED", key, "system")
        return tuple(expired)

    def add_corridor(self, corridor: Corridor) -> None:
        if corridor.from_branch_id == corridor.to_branch_id or not corridor.zone_ids:
            raise ValueError("VALID_CORRIDOR_REQUIRED")
        self._corridors[corridor.corridor_id] = corridor

    def approve_corridor(self, corridor_id: str, *, branch_id: str) -> Corridor:
        value = self._corridors[corridor_id]
        if branch_id not in {value.from_branch_id, value.to_branch_id}:
            raise ControlRejected(ControlError.ACCESS_DENIED)
        updated = replace(value, approvals=value.approvals | {branch_id})
        self._corridors[corridor_id] = updated
        return updated

    def transfer(self, corridor_id: str, *, from_branch_id: str, to_branch_id: str,
                 zone_id: str, payload: dict[str, object]) -> None:
        value = self._corridors[corridor_id]
        if (from_branch_id != value.from_branch_id or to_branch_id != value.to_branch_id
                or zone_id not in value.zone_ids
                or value.approvals != {from_branch_id, to_branch_id}):
            raise ControlRejected(ControlError.CORRIDOR_APPROVAL_REQUIRED)
        if any(key.lower() in {"address", "phone", "name", "lat", "lng"} for key in payload):
            raise ControlRejected(ControlError.RAW_PII_FORBIDDEN)
        self._emit(from_branch_id, "CORRIDOR_TRANSFER_AUTHORIZED", corridor_id, "operator")

    def arkaon(self, action: str) -> None:
        if action not in {"AGGREGATE_FORECAST", "RECOMMEND"}:
            raise ControlRejected(ControlError.AI_AUTHORITY_FORBIDDEN)

    def _branch(self, branch_id: str) -> BranchNode:
        try:
            return self.branches[branch_id]
        except KeyError as exc:
            raise ControlRejected(ControlError.ACCESS_DENIED) from exc

    def _emit(self, branch_id: str, action: str, subject: str, actor: str) -> None:
        event = ControlAudit(len(self.audit) + 1, branch_id, action, subject, actor)
        self.audit.append(event)
        self.outbox.append(event)
