import pytest

from narang_rider.contributor_guidance import (
    ArkaonContributorGuide,
    GuidancePriority,
    GuidanceRejected,
)


def item(report, rule_id):
    return next(value for value in report.items if value.rule_id == rule_id)


def test_source_change_preemptively_requests_tests():
    report = ArkaonContributorGuide().analyze(("src/narang_rider/orders.py",))
    finding = item(report, "SOURCE_REQUIRES_TEST")
    assert finding.priority is GuidancePriority.REQUIRED
    assert not finding.satisfied
    assert report.required_open_count == 1


def test_companion_test_satisfies_source_guidance_without_approving_change():
    report = ArkaonContributorGuide().analyze(
        ("src/narang_rider/orders.py", "tests/test_orders.py")
    )
    assert item(report, "SOURCE_REQUIRES_TEST").satisfied
    assert report.required_open_count == 0
    assert report.advisory_only
    assert not report.automatic_approval_allowed


def test_policy_workflow_and_contract_changes_receive_targeted_guidance():
    report = ArkaonContributorGuide().analyze(
        (
            "config/mobility-plaza-contract-template.json",
            ".github/workflows/ci.yml",
            "tests/test_contract.py",
            "docs/contract.md",
        )
    )
    assert item(report, "POLICY_REQUIRES_EVIDENCE").satisfied
    assert item(report, "WORKFLOW_REQUIRES_TEST_OR_DOC").satisfied
    assert item(report, "CONTRACT_REQUIRES_HUMAN_REVIEW").satisfied


def test_frontend_guidance_is_recommended_and_mentions_accessibility():
    report = ArkaonContributorGuide().analyze(("frontend/src/app.tsx",))
    finding = item(report, "FRONTEND_REQUIRES_ACCESSIBILITY")
    assert finding.priority is GuidancePriority.RECOMMENDED
    assert "접근성" in finding.title


def test_paths_are_deduplicated_sorted_and_digest_is_deterministic():
    guide = ArkaonContributorGuide()
    first = guide.analyze(("tests/test_x.py", "src/x.py", "src/x.py"))
    second = guide.analyze(("src/x.py", "tests/test_x.py"))
    assert first.changed_paths == ("src/x.py", "tests/test_x.py")
    assert first.report_digest == second.report_digest


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", ".git/config", ""])
def test_unsafe_or_internal_git_paths_are_rejected(path):
    with pytest.raises(GuidanceRejected, match="safe"):
        ArkaonContributorGuide().analyze((path,))


def test_no_visitor_tracking_and_no_arkaon_approval_authority():
    guide = ArkaonContributorGuide()
    report = guide.analyze(("README.md",))
    assert not report.visitor_tracking_used
    with pytest.raises(GuidanceRejected, match="approve or merge"):
        guide.approve_or_merge()
