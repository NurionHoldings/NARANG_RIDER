import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from narang_rider.incident_api import IncidentApi, IncidentHttpRequest
from narang_rider.incident_support import (
    ArkaonIncidentAction,
    CoverageSnapshot,
    IncidentErrorCode,
    IncidentKind,
    IncidentPrincipal,
    IncidentRejected,
    IncidentReportCommand,
    IncidentRole,
    IncidentService,
    SafetyStopReceipt,
)


class Stops:
    def __init__(self):
        self.calls = []

    def stop(self, rider_id, branch_id, reason_ref):
        self.calls.append((rider_id, branch_id, reason_ref))
        return SafetyStopReceipt(rider_id)


class Grants:
    def __init__(self):
        self.value = "grant_once_123"

    def issue(self, case_id, rider_id, purpose):
        return self.value


class Handoff:
    def handoff(self, channel, case):
        return f"external/{channel.lower()}/123"


@pytest.fixture
def setup():
    stops = Stops()
    grants = Grants()
    service = IncidentService(stops, grants, Handoff())
    safety = IncidentPrincipal("safety_1", "branch_1", IncidentRole.BRANCH_SAFETY)
    rider = IncidentPrincipal("rider_1", "branch_1", IncidentRole.RIDER)
    specialist = IncidentPrincipal("specialist_1", "branch_1", IncidentRole.INSURANCE_SPECIALIST)
    coverage = CoverageSnapshot(
        "coverage_1",
        "assignment_1",
        "rider_1",
        "branch_1",
        "provider/ref_1",
        "product/ref_1",
        datetime.now(UTC),
    )
    service.capture_coverage(safety, coverage)
    command = IncidentReportCommand(
        "assignment_1",
        IncidentKind.ACCIDENT,
        "idem_123",
        "sejong-zone-2",
        "vault://incident/narrative/1",
        "vault://health/1",
    )
    return service, stops, grants, safety, rider, specialist, command


def test_report_stops_dispatch_without_penalty_and_preserves_earnings(setup):
    service, stops, _, _, rider, _, command = setup
    case, stop, earnings = service.report(rider, command)
    assert case.coverage_snapshot_id == "coverage_1"
    assert stop.dispatch_blocked and not stop.decline_penalty_allowed
    assert not stop.retaliation_signal_allowed
    assert earnings.undisputed_earnings_payable
    assert not earnings.automatic_hold_allowed
    assert not earnings.automatic_clawback_allowed
    assert stops.calls[0][0] == "rider_1"


def test_report_is_idempotent_but_duplicate_or_changed_payload_is_blocked(setup):
    service, _, _, _, rider, _, command = setup
    first = service.report(rider, command)[0]
    assert service.report(rider, command)[0] == first
    with pytest.raises(IncidentRejected) as error:
        service.report(rider, replace(command, coarse_zone="other-zone"))
    assert error.value.code is IncidentErrorCode.DUPLICATE_INCIDENT
    with pytest.raises(IncidentRejected):
        service.report(rider, replace(command, idempotency_key="different_1"))


def test_raw_health_data_and_forged_coverage_are_rejected(setup):
    service, _, _, _, rider, _, command = setup
    with pytest.raises(IncidentRejected) as raw:
        service.report(rider, replace(command, medical_vault_ref="broken arm"))
    assert raw.value.code is IncidentErrorCode.RAW_SENSITIVE_DATA
    with pytest.raises(IncidentRejected) as forged:
        service.report(rider, replace(command, assignment_id="assignment_fake"))
    assert forged.value.code is IncidentErrorCode.COVERAGE_FORGED


def test_idor_branch_and_rider_ownership(setup):
    service, _, _, safety, rider, _, command = setup
    case = service.report(rider, command)[0]
    with pytest.raises(IncidentRejected):
        service.get(IncidentPrincipal("rider_2", "branch_1", IncidentRole.RIDER), case.case_id)
    with pytest.raises(IncidentRejected) as branch:
        service.get(replace(safety, branch_id="branch_2"), case.case_id)
    assert branch.value.code is IncidentErrorCode.BRANCH_SCOPE


def test_medical_reference_is_role_restricted_and_audited(setup):
    service, _, _, safety, rider, specialist, command = setup
    case = service.report(rider, command)[0]
    assert service.get(safety, case.case_id).medical_vault_ref is None
    assert service.get(specialist, case.case_id).medical_vault_ref == "vault://health/1"
    assert service.audit[-1]["sensitive_access"] == "true"


def test_human_workflow_handoff_resolution_appeal_and_correction(setup):
    service, _, _, safety, rider, specialist, command = setup
    case = service.report(rider, command)[0]
    assert service.triage(safety, case.case_id).state.value == "TRIAGED"
    assigned = service.assign_human(safety, case.case_id, "human_adjuster_1")
    submitted = service.submit(specialist, case.case_id, "INSURER")
    assert assigned.assigned_human_id and submitted.external_case_ref
    resolved = service.resolve(specialist, case.case_id, "resolution/ref_1")
    appealed = service.appeal(rider, case.case_id, "vault://appeal/reason/1")
    corrected = service.correct(specialist, case.case_id, "correction/ref_1")
    assert resolved.resolution_ref and appealed.appeal_reason_vault_ref
    assert corrected.correction_ref == "correction/ref_1"


def test_premature_closure_and_photo_only_liability_are_impossible(setup):
    service, _, _, _, rider, specialist, command = setup
    case = service.report(rider, command)[0]
    with pytest.raises(IncidentRejected) as error:
        service.resolve(specialist, case.case_id, "photo/says/fault")
    assert error.value.code is IncidentErrorCode.INVALID_STATE
    assert not hasattr(service, "determine_liability")


@pytest.mark.parametrize(
    "action",
    [
        ArkaonIncidentAction.DETERMINE_FAULT,
        ArkaonIncidentAction.DENY_COVERAGE,
        ArkaonIncidentAction.PRICE_PREMIUM,
        ArkaonIncidentAction.SUSPEND_ACCOUNT,
        ArkaonIncidentAction.CLAW_BACK_PAY,
        ArkaonIncidentAction.AFFECT_DISPATCH,
    ],
)
def test_arkaon_forbidden_decisions_fail_closed(setup, action):
    service, _, _, safety, rider, _, command = setup
    case = service.report(rider, command)[0]
    with pytest.raises(IncidentRejected) as error:
        service.arkaon(safety, case.case_id, action)
    assert error.value.code is IncidentErrorCode.ARKAON_AUTHORITY_DENIED


def test_arkaon_only_summarizes_or_checks_missing_documents(setup):
    service, _, _, safety, rider, _, command = setup
    case = service.report(rider, command)[0]
    result = service.arkaon(safety, case.case_id, ArkaonIncidentAction.SUMMARIZE)
    assert result["advisory_only"] and result["human_review_required"]


def test_evidence_grants_are_scoped_and_single_use(setup):
    service, _, grants, _, rider, _, command = setup
    case = service.report(rider, command)[0]
    assert service.evidence_grant(rider, case.case_id, "DAMAGE") == "grant_once_123"
    with pytest.raises(IncidentRejected) as reused:
        service.evidence_grant(rider, case.case_id, "DOCUMENT")
    assert reused.value.code is IncidentErrorCode.EVIDENCE_REUSED
    grants.value = "grant_once_456"
    with pytest.raises(IncidentRejected):
        service.evidence_grant(replace(rider, actor_id="rider_2"), case.case_id, "DAMAGE")


def test_api_report_and_no_client_role_trust(setup):
    service, _, _, _, rider, _, _ = setup
    api = IncidentApi(service)
    body = {
        "assignment_id": "assignment_1",
        "kind": "INJURY",
        "coarse_zone": "zone-2",
        "narrative_vault_ref": "vault://incident/narrative/2",
        "medical_vault_ref": "vault://health/2",
        "role": "INSURANCE_SPECIALIST",
    }
    rejected = api.handle(
        rider,
        IncidentHttpRequest(
            "POST",
            "/api/v1/rider-incidents",
            {"Content-Type": "application/json", "Idempotency-Key": "idem_222"},
            json.dumps(body).encode(),
        ),
    )
    assert rejected.status == 400
    body.pop("role")
    accepted = api.handle(
        rider,
        IncidentHttpRequest(
            "POST",
            "/api/v1/rider-incidents",
            {"Content-Type": "application/json", "Idempotency-Key": "idem_222"},
            json.dumps(body).encode(),
        ),
    )
    assert accepted.status == 201
    assert accepted.body["earnings_protection"]["undisputed_earnings_payable"]


def test_coverage_snapshot_is_immutable_and_cannot_be_overwritten(setup):
    service, _, _, safety, _, _, _ = setup
    forged = CoverageSnapshot(
        "coverage_2",
        "assignment_1",
        "rider_1",
        "branch_1",
        "provider/ref_fake",
        "product/ref_fake",
        datetime.now(UTC),
    )
    with pytest.raises(IncidentRejected) as error:
        service.capture_coverage(safety, forged)
    assert error.value.code is IncidentErrorCode.COVERAGE_FORGED
