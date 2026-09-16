from datetime import UTC, datetime, timedelta

import pytest

from narang_rider.professional_review import (
    DecisionStatus,
    Discipline,
    OperatorAcceptance,
    ProfessionalDecision,
    ProfessionalDecisionRegistry,
    ProfessionalReviewRejected,
    admin_readiness_view,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)
VERSION = "0.1.0-rc.5+#060"
SCOPE = "national-sandbox-release"


def decision(discipline, **changes):
    values = {
        "decision_id": f"decision-{discipline.value}",
        "discipline": discipline,
        "reviewer_identity_ref": f"professional-registry://reviewer-{discipline.value}",
        "signed_approval_ref": f"signed-approval://decision/{discipline.value}/opaque-signature",
        "scope": SCOPE,
        "system_version": VERSION,
        "effective_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=90),
        "status": DecisionStatus.ACCEPTED,
    }
    values.update(changes)
    return ProfessionalDecision(**values)


def acceptance(**changes):
    values = {
        "operator_identity_ref": "operator-registry://choi-inseok",
        "signed_acceptance_ref": "signed-approval://operator/release/opaque-signature",
        "scope": SCOPE,
        "system_version": VERSION,
        "accepted_at": NOW,
    }
    values.update(changes)
    return OperatorAcceptance(**values)


def full_registry():
    registry = ProfessionalDecisionRegistry(system_version=VERSION, scope=SCOPE, now=NOW)
    for discipline in Discipline:
        registry.record(decision(discipline))
    return registry


def test_all_independent_disciplines_and_operator_are_required():
    registry = full_registry()
    readiness = registry.readiness(acceptance())
    assert readiness.legal_approval is True
    assert readiness.release_status == "BLOCKED"
    view = admin_readiness_view(readiness)
    assert view["legal_approval"] is True
    assert "정부" in view["notice"]


def test_partial_disciplines_never_approve():
    registry = ProfessionalDecisionRegistry(system_version=VERSION, scope=SCOPE, now=NOW)
    registry.record(decision(Discipline.COUNSEL_PRIVACY))
    readiness = registry.readiness(acceptance())
    assert readiness.legal_approval is False
    assert Discipline.LOCATION in readiness.missing_disciplines


@pytest.mark.parametrize(
    "changes",
    [
        {"signed_approval_ref": "not-signed"},
        {"expires_at": NOW},
        {"scope": "another-scope"},
        {"system_version": "changed-version"},
        {"conflict_refs": ("conflict://financial-interest",)},
    ],
)
def test_forged_expired_scope_version_and_conflict_are_rejected(changes):
    registry = ProfessionalDecisionRegistry(system_version=VERSION, scope=SCOPE, now=NOW)
    with pytest.raises(ProfessionalReviewRejected):
        registry.record(decision(Discipline.COUNSEL_PRIVACY, **changes))


def test_conditional_rejected_and_withdrawn_gate_release():
    conditional = ProfessionalDecisionRegistry(system_version=VERSION, scope=SCOPE, now=NOW)
    for discipline in Discipline:
        item = decision(discipline)
        if discipline is Discipline.LOCATION:
            item = decision(
                discipline,
                status=DecisionStatus.CONDITIONAL,
                conditions=("location terms revision required",),
            )
        conditional.record(item)
    assert conditional.readiness(acceptance()).legal_approval is False

    rejected = full_registry()
    rejected.record(
        decision(
            Discipline.LOCATION,
            decision_id="decision-location-rejected",
            status=DecisionStatus.REJECTED,
        )
    )
    assert rejected.readiness(acceptance()).legal_approval is False

    withdrawn = full_registry()
    withdrawn.withdraw(
        Discipline.LOCATION,
        withdrawn_at=NOW,
        withdrawal_ref="signed-approval://withdrawal/location/opaque-signature",
    )
    assert withdrawn.readiness(acceptance()).legal_approval is False


def test_duplicate_reviewer_and_operator_self_approval_are_blocked():
    registry = ProfessionalDecisionRegistry(system_version=VERSION, scope=SCOPE, now=NOW)
    shared = "professional-registry://same-person"
    for discipline in Discipline:
        registry.record(decision(discipline, reviewer_identity_ref=shared))
    assert registry.readiness(acceptance()).legal_approval is False

    independent = full_registry()
    self_acceptance = acceptance(
        operator_identity_ref="professional-registry://reviewer-counsel_privacy"
    )
    assert independent.readiness(self_acceptance).legal_approval is False


def test_changed_system_version_invalidates_previous_packet():
    registry = full_registry()
    registry.system_version = "0.1.0-rc.6"
    readiness = registry.readiness(acceptance(system_version="0.1.0-rc.6"))
    assert readiness.legal_approval is False
