from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from narang_rider.predeployment_readiness import (
    ArkaonPredeploymentInspector,
    CheckEvidence,
    IntegrationCandidate,
    ReadinessRejected,
    ReadinessVerdict,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)
HEAD = "a9bc9fcd6fdf87cf17db0daee77459dcf8fc5beb"


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def candidate() -> IntegrationCandidate:
    return IntegrationCandidate(
        "rc.13-predeployment",
        HEAD,
        "8ea1e56774d9de350b9b8455e58fa98a95f94c14",
        tuple(range(1, 64)) + (72, 73, 75, 76, 78, 79, 80, 81, 82, 83, 85, 86, 87, 88),
        digest("source-tree"),
    )


def evidence(check_id: str, status: str = "PASS") -> CheckEvidence:
    return CheckEvidence(
        check_id,
        "TEST",
        status,
        digest(check_id),
        NOW - timedelta(minutes=1),
        NOW + timedelta(days=1),
        HEAD,
    )


def all_evidence() -> tuple[CheckEvidence, ...]:
    return tuple(evidence(item) for item in sorted(ArkaonPredeploymentInspector.REQUIRED_CHECKS))


def test_complete_evidence_only_reaches_operator_review_never_merge_or_deploy():
    report = ArkaonPredeploymentInspector().inspect(
        report_id="report-1", candidate=candidate(), evidence=all_evidence(), now=NOW
    )
    assert report.verdict is ReadinessVerdict.READY_FOR_OPERATOR_REVIEW
    assert report.device_test_ready
    assert not report.merge_recommended
    assert not report.deploy_allowed
    assert not report.findings


def test_missing_device_evidence_blocks_and_assigns_remediation_owner():
    values = tuple(item for item in all_evidence() if item.check_id != "SIGNED_TEST_BUILD")
    report = ArkaonPredeploymentInspector().inspect(
        report_id="report-2", candidate=candidate(), evidence=values, now=NOW
    )
    assert report.verdict is ReadinessVerdict.BLOCKED
    assert not report.device_test_ready
    finding = next(item for item in report.findings if "SIGNED_TEST_BUILD" in item.title)
    assert finding.owner_role == "RELEASE_ENGINEER"
    assert finding.operator_decision_required


@pytest.mark.parametrize("status", ["FAIL", "MISSING", "STALE"])
def test_failed_missing_or_stale_check_is_never_silently_waived(status):
    values = tuple(
        replace(item, status=status) if item.check_id == "ROLLBACK_DRILL" else item
        for item in all_evidence()
    )
    report = ArkaonPredeploymentInspector().inspect(
        report_id=f"report-{status}", candidate=candidate(), evidence=values, now=NOW
    )
    assert report.verdict is ReadinessVerdict.BLOCKED
    assert any(status in item.title for item in report.findings)


def test_evidence_from_another_commit_is_rejected_as_failing():
    values = tuple(
        replace(item, commit_sha="f" * 40)
        if item.check_id == "UNIT_REGRESSION"
        else item
        for item in all_evidence()
    )
    report = ArkaonPredeploymentInspector().inspect(
        report_id="report-commit", candidate=candidate(), evidence=values, now=NOW
    )
    assert report.verdict is ReadinessVerdict.BLOCKED
    assert any("UNIT_REGRESSION: FAIL" == item.title for item in report.findings)


def test_duplicate_or_unknown_evidence_fails_closed():
    inspector = ArkaonPredeploymentInspector()
    duplicated = all_evidence() + (all_evidence()[0],)
    with pytest.raises(ReadinessRejected, match="duplicate"):
        inspector.inspect(report_id="bad", candidate=candidate(), evidence=duplicated, now=NOW)
    with pytest.raises(ReadinessRejected, match="unknown"):
        inspector.inspect(
            report_id="bad",
            candidate=candidate(),
            evidence=all_evidence() + (evidence("INVENTED"),),
            now=NOW,
        )


def test_arkaon_cannot_accept_remediation_or_authorize_release():
    inspector = ArkaonPredeploymentInspector()
    with pytest.raises(ReadinessRejected, match="own remediation"):
        inspector.accept_resolution("finding")
    with pytest.raises(ReadinessRejected, match="merge or deployment"):
        inspector.authorize_merge_or_deploy()


def test_report_digest_is_deterministic():
    inspector = ArkaonPredeploymentInspector()
    first = inspector.inspect(
        report_id="report-stable", candidate=candidate(), evidence=all_evidence(), now=NOW
    )
    second = inspector.inspect(
        report_id="report-stable", candidate=candidate(), evidence=all_evidence(), now=NOW
    )
    assert first.report_digest == second.report_digest
