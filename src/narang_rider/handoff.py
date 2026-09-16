"""Executable, synthetic-only operational handoff and tabletop controls."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class OnCallRole(StrEnum):
    HQ_INCIDENT_COMMANDER = "HQ_INCIDENT_COMMANDER"
    REGIONAL_OPERATOR = "REGIONAL_OPERATOR"
    LOCAL_OPERATOR = "LOCAL_OPERATOR"
    SECURITY_REVIEWER = "SECURITY_REVIEWER"
    FINANCE_REVIEWER = "FINANCE_REVIEWER"
    PRIVACY_OFFICER = "PRIVACY_OFFICER"
    SAFETY_OFFICER = "SAFETY_OFFICER"
    OPERATOR_APPROVER = "OPERATOR_APPROVER"
    ARKAON_ADVISOR = "ARKAON_ADVISOR"


class IncidentSeverity(StrEnum):
    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"


class ScenarioKind(StrEnum):
    AUTH_JWKS = "AUTH_JWKS"
    DB_RESTORE = "DB_RESTORE"
    OUTBOX_PARTNER = "OUTBOX_PARTNER"
    SETTLEMENT_PAYOUT = "SETTLEMENT_PAYOUT"
    PRIVACY_DSAR = "PRIVACY_DSAR"
    EVIDENCE_MEDIA = "EVIDENCE_MEDIA"
    MAP_NOTIFICATION = "MAP_NOTIFICATION"
    SAFETY_INSURANCE = "SAFETY_INSURANCE"
    CAPACITY_OVERLOAD = "CAPACITY_OVERLOAD"
    ROLLBACK = "ROLLBACK"


@dataclass(frozen=True)
class Runbook:
    kind: ScenarioKind
    severity: IncidentSeverity
    detection_signals: tuple[str, ...]
    contain_actions: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    approval_roles: tuple[OnCallRole, ...]
    evidence_refs: tuple[str, ...]
    route_refs: tuple[str, ...]
    recovery_criteria: tuple[str, ...]
    postmortem_fields: tuple[str, ...] = (
        "timeline", "impact", "root_cause", "rights_impact", "corrective_actions"
    )


COMMON_FORBIDDEN = (
    "AUTO_MERGE_OR_DEPLOY",
    "DELETE_OR_REWRITE_LEDGER",
    "AUTOMATIC_RIDER_CLAWBACK",
    "USE_RAW_PII_IN_CHAT_OR_LOG",
    "ARKAON_UNILATERAL_RESTART",
)


def _runbook(
    kind: ScenarioKind,
    severity: IncidentSeverity,
    signals: tuple[str, ...],
    actions: tuple[str, ...],
    approvals: tuple[OnCallRole, ...],
    evidence: tuple[str, ...],
    routes: tuple[str, ...] = (),
) -> Runbook:
    return Runbook(
        kind, severity, signals, actions, COMMON_FORBIDDEN, approvals, evidence, routes,
        ("ROOT_CAUSE_REMOVED", "SYNTHETIC_PROOF_PASS", "INDEPENDENT_HUMAN_APPROVAL"),
    )


RUNBOOKS = {
    ScenarioKind.AUTH_JWKS: _runbook(
        ScenarioKind.AUTH_JWKS, IncidentSeverity.SEV1,
        ("JWKS_REFRESH_FAILURE", "AUTH_FAILURE_RATE"),
        ("FAIL_CLOSED", "PAUSE_PRIVILEGED_WRITES", "USE_APPROVED_KEY_CACHE"),
        (OnCallRole.SECURITY_REVIEWER, OnCallRole.OPERATOR_APPROVER),
        ("src/narang_rider/authentication.py", "docs/05-release-security-and-recovery.md"),
        ("/api/v1/session",),
    ),
    ScenarioKind.DB_RESTORE: _runbook(
        ScenarioKind.DB_RESTORE, IncidentSeverity.SEV1,
        ("DB_READINESS_DOWN", "SERIALIZATION_FAILURE_SURGE"),
        ("STOP_NEW_FINANCIAL_WRITES", "PRESERVE_OUTBOX", "RESTORE_SYNTHETIC_FIRST"),
        (OnCallRole.HQ_INCIDENT_COMMANDER, OnCallRole.OPERATOR_APPROVER),
        ("migrations/0001_postgres_persistence.sql", "src/narang_rider/release_security.py"),
    ),
    ScenarioKind.OUTBOX_PARTNER: _runbook(
        ScenarioKind.OUTBOX_PARTNER, IncidentSeverity.SEV2,
        ("DLQ_GROWTH", "PARTNER_CIRCUIT_OPEN"),
        ("ISOLATE_PARTNER", "KEEP_STREAM_ORDER", "HUMAN_REVIEW_DLQ"),
        (OnCallRole.REGIONAL_OPERATOR, OnCallRole.OPERATOR_APPROVER),
        ("src/narang_rider/outbox_worker.py", "src/narang_rider/partner_connectors.py"),
    ),
    ScenarioKind.SETTLEMENT_PAYOUT: _runbook(
        ScenarioKind.SETTLEMENT_PAYOUT, IncidentSeverity.SEV1,
        ("AMOUNT_MISMATCH", "CALLBACK_REPLAY"),
        ("HOLD_DISPUTED_ITEM_ONLY", "PAY_UNDISPUTED_AMOUNT", "HUMAN_RECONCILE"),
        (OnCallRole.FINANCE_REVIEWER, OnCallRole.OPERATOR_APPROVER),
        ("src/narang_rider/settlement_operations.py", "migrations/0002_settlement_operations.sql"),
        ("/api/v1/payout-instructions/{instruction_id}/approvals",),
    ),
    ScenarioKind.PRIVACY_DSAR: _runbook(
        ScenarioKind.PRIVACY_DSAR, IncidentSeverity.SEV1,
        ("PRIVACY_BREACH_SIGNAL", "DSAR_DEADLINE_RISK"),
        ("CONTAIN_REFERENCE", "PRESERVE_MINIMAL_AUDIT", "HUMAN_BREACH_ASSESSMENT"),
        (OnCallRole.PRIVACY_OFFICER, OnCallRole.OPERATOR_APPROVER),
        ("src/narang_rider/privacy.py", "docs/16-privacy-rights-retention.md"),
    ),
    ScenarioKind.EVIDENCE_MEDIA: _runbook(
        ScenarioKind.EVIDENCE_MEDIA, IncidentSeverity.SEV2,
        ("MASKING_FAILURE", "MALWARE_SCAN_FAILURE"),
        ("STOP_MEDIA_PERSIST", "ALLOW_NO_CAPTURE_REASON", "PRESERVE_RIDER_PAY"),
        (OnCallRole.PRIVACY_OFFICER, OnCallRole.LOCAL_OPERATOR),
        ("src/narang_rider/evidence.py", "src/narang_rider/customer_proof.py"),
    ),
    ScenarioKind.MAP_NOTIFICATION: _runbook(
        ScenarioKind.MAP_NOTIFICATION, IncidentSeverity.SEV2,
        ("MAP_PROVIDER_TIMEOUT", "NOTIFICATION_PROVIDER_DOWN"),
        ("USE_CONSERVATIVE_QUOTE", "MINIMAL_FALLBACK_NOTICE", "NO_PRECISE_LOCATION"),
        (OnCallRole.REGIONAL_OPERATOR, OnCallRole.LOCAL_OPERATOR),
        ("src/narang_rider/geospatial.py", "src/narang_rider/notifications.py"),
    ),
    ScenarioKind.SAFETY_INSURANCE: _runbook(
        ScenarioKind.SAFETY_INSURANCE, IncidentSeverity.SEV1,
        ("RIDER_SAFETY_STOP", "COVERAGE_LOOKUP_FAILURE"),
        ("STOP_ASSIGNMENT", "NO_DECLINE_PENALTY", "PRESERVE_UNDISPUTED_EARNINGS"),
        (OnCallRole.SAFETY_OFFICER, OnCallRole.OPERATOR_APPROVER),
        ("src/narang_rider/incident_support.py", "migrations/0004_rider_incident_support.sql"),
    ),
    ScenarioKind.CAPACITY_OVERLOAD: _runbook(
        ScenarioKind.CAPACITY_OVERLOAD, IncidentSeverity.SEV2,
        ("QUEUE_AGE_BUDGET", "HTTP_429_RATE"),
        ("UNIFORM_LOAD_SHED", "RETRY_AFTER", "PROTECT_SAFETY_AND_SETTLEMENT"),
        (OnCallRole.HQ_INCIDENT_COMMANDER, OnCallRole.REGIONAL_OPERATOR),
        ("src/narang_rider/resource_limits.py", "docs/10-performance-resource-audit.md"),
    ),
    ScenarioKind.ROLLBACK: _runbook(
        ScenarioKind.ROLLBACK, IncidentSeverity.SEV1,
        ("RELEASE_INVARIANT_FAILURE", "ERROR_BUDGET_EXHAUSTED"),
        ("PAUSE_ROLLOUT", "ROLLBACK_CODE_ONLY", "VERIFY_SCHEMA_COMPATIBILITY"),
        (OnCallRole.HQ_INCIDENT_COMMANDER, OnCallRole.OPERATOR_APPROVER),
        ("src/narang_rider/release_candidate.py", "docs/18-release-candidate-handoff.md"),
    ),
}


@dataclass(frozen=True)
class DrillAction:
    actor_id: str
    role: OnCallRole
    branch_id: str
    action: str
    fallback_payload_keys: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DrillResult:
    passed: bool
    violations: tuple[str, ...]


class TabletopEngine:
    SENSITIVE_FALLBACK_KEYS = frozenset(
        {"address", "phone", "latitude", "longitude", "rider_id", "order_contents"}
    )

    def evaluate(
        self, *, scenario: ScenarioKind, incident_branch_id: str,
        actions: tuple[DrillAction, ...], regional_prefix: str = "",
    ) -> DrillResult:
        runbook = RUNBOOKS[scenario]
        violations: list[str] = []
        approvals: dict[OnCallRole, str] = {}
        for item in actions:
            if item.role is OnCallRole.ARKAON_ADVISOR and item.action not in {"RECOMMEND", "EXPLAIN"}:
                violations.append("AI_UNILATERAL_AUTHORITY")
            if item.action in runbook.forbidden_actions:
                violations.append(item.action)
            if item.action == "RIDER_CLAWBACK":
                violations.append("AUTOMATIC_RIDER_CLAWBACK")
            if item.role is OnCallRole.LOCAL_OPERATOR and item.branch_id != incident_branch_id:
                violations.append("BRANCH_SCOPE_VIOLATION")
            if item.role is OnCallRole.REGIONAL_OPERATOR and regional_prefix and not incident_branch_id.startswith(regional_prefix):
                violations.append("BRANCH_SCOPE_VIOLATION")
            if item.fallback_payload_keys & self.SENSITIVE_FALLBACK_KEYS:
                violations.append("FALLBACK_PRIVACY_VIOLATION")
            if item.action == "APPROVE_RECOVERY":
                approvals[item.role] = item.actor_id
        required = set(runbook.approval_roles)
        if actions and not required.issubset(approvals):
            violations.append("APPROVALS_INCOMPLETE")
        if len(set(approvals.values())) != len(approvals):
            violations.append("SELF_OR_DUPLICATE_APPROVAL")
        return DrillResult(not violations, tuple(dict.fromkeys(violations)))


def verify_handoff_references(root: Path) -> tuple[str, ...]:
    missing: list[str] = []
    source_paths = tuple((root / "src/narang_rider").glob("*.py")) + tuple(
        (root / "frontend/src").glob("*.ts")
    )
    source = "\n".join(path.read_text() for path in source_paths)
    for runbook in RUNBOOKS.values():
        for reference in runbook.evidence_refs:
            if not (root / reference).is_file():
                missing.append(reference)
        for route in runbook.route_refs:
            if route not in source:
                missing.append(route)
    return tuple(sorted(set(missing)))
