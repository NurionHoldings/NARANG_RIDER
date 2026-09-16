from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from narang_rider.experience_operations_audit import (
    ArkaonExperienceOperationsAuditor,
    AuditRejected,
    BenchmarkFeature,
    FindingSeverity,
    OperationalManifest,
    PageSnapshot,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)
HEAD = "e2dd9f5d55e5a3a953af565ca467bd0aa4d1c875"


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def page(**changes):
    values = {"page_id": "home", "path": "/", "title": "나랑라이더",
        "heading_levels": (1, 2), "primary_actions": ("/start",),
        "visible_texts": ("나랑 달리고, 나란히 성장하다.",),
        "navigation_targets": ("/start",), "average_contrast_ratio": 7.0,
        "minimum_touch_target_px": 44, "mobile_overflow": False}
    values.update(changes)
    return PageSnapshot(**values)


def manifest(**changes):
    values = {"frontend_routes": frozenset({"/", "/start"}),
        "api_routes": frozenset({"/", "/start"}),
        "policy_route_refs": frozenset({"/start"}),
        "required_home_actions": frozenset({"/start"})}
    values.update(changes)
    return OperationalManifest(**values)


def benchmark():
    return BenchmarkFeature("b1", "https://official.example/release", "공식 발표", "공식사",
        NOW, NOW + timedelta(days=14), digest("evidence"), "단계형 신청", "진행상태 표시", True)


def audit(pages=None, value=None, benchmarks=()):
    return ArkaonExperienceOperationsAuditor().audit(report_id="r1", candidate_commit=HEAD,
        pages=pages or (page(), page(page_id="start", path="/start", primary_actions=("submit",), navigation_targets=())),
        manifest=value or manifest(), benchmarks=benchmarks, now=NOW)


def test_clean_home_and_routes_have_no_operational_findings():
    assert audit().findings == ()


def test_readability_content_heading_and_action_problems_are_proposed_immediately():
    bad = page(title="", heading_levels=(2,), primary_actions=("a", "b"),
        visible_texts=("긴문장" * 40,), average_contrast_ratio=3.0, minimum_touch_target_px=32,
        mobile_overflow=True)
    report = audit(pages=(bad, page(page_id="start", path="/start", primary_actions=("submit",), navigation_targets=())))
    assert {item.category for item in report.findings} >= {"HEADING", "PRIMARY_ACTION", "READABILITY", "CONTENT"}
    assert report.immediate_proposal_count == len(report.findings)
    assert not report.automatic_change_allowed


def test_broken_navigation_and_route_contract_mismatch_are_blockers():
    report = audit(pages=(page(navigation_targets=("/missing",)),),
        value=manifest(frontend_routes=frozenset({"/", "/start"}), api_routes=frozenset({"/"})))
    blockers = [item for item in report.findings if item.severity is FindingSeverity.BLOCKER]
    assert blockers and {item.category for item in blockers} >= {"BROKEN_PATH", "FRONTEND_API"}


def test_official_benchmark_becomes_non_copy_human_review_proposal():
    report = audit(benchmarks=(benchmark(),))
    finding = next(item for item in report.findings if item.category == "BENCHMARK")
    assert finding.requires_human_review
    assert "독자적인 합성 초안" in finding.recommendation
    assert not report.competitor_copy_allowed


def test_unofficial_stale_or_copyable_benchmark_is_rejected():
    for bad in (replace(benchmark(), official=False), replace(benchmark(), expires_at=NOW),
                replace(benchmark(), copy_prohibited=False)):
        with pytest.raises(AuditRejected, match="official non-copy"):
            audit(benchmarks=(bad,))


def test_arkaon_cannot_apply_its_own_improvement():
    with pytest.raises(AuditRejected, match="cannot apply"):
        ArkaonExperienceOperationsAuditor().apply_improvement()
