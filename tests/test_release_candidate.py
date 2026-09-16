from datetime import UTC, datetime, timedelta

from narang_rider.release_candidate import (
    FINAL_SMOKE_CHECKLIST,
    ChangeFreezePolicy,
    EvidenceItem,
    EvidenceStatus,
    FreezeException,
    ReleaseCandidateEvaluator,
    ReleaseRequirement,
    ReleaseVerdict,
    TraceabilityVerifier,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def requirement(**changes):
    values = {
        "requirement_id": "REQ-SAFETY",
        "title": "안전과 법적 하한",
        "domain_refs": ("domain:safety",),
        "api_refs": ("api:safety",),
        "db_refs": ("db:safety",),
        "frontend_refs": ("ui:safety",),
        "test_refs": ("test:safety",),
        "threat_controls": ("threat:safety",),
    }
    values.update(changes)
    return ReleaseRequirement(**values)


def refs(item):
    return {
        ref for field in ("domain_refs", "api_refs", "db_refs", "frontend_refs",
                          "test_refs", "threat_controls") for ref in getattr(item, field)
    }


def test_traceability_fails_missing_layer_and_orphan_reference():
    item = requirement(api_refs=())
    report = TraceabilityVerifier.verify([item], known_refs=refs(item) - {"ui:safety"})
    assert report.missing_coverage == {"REQ-SAFETY": ("api",)}
    assert report.orphan_refs == ("ui:safety",)
    assert not report.complete


def test_traceability_complete_has_stable_digest():
    item = requirement()
    one = TraceabilityVerifier.verify([item], known_refs=refs(item))
    two = TraceabilityVerifier.verify([item], known_refs=tuple(refs(item))[::-1])
    assert one.complete and one.digest == two.digest


def test_release_is_blocked_without_external_and_governance_evidence():
    item = requirement()
    trace = TraceabilityVerifier.verify([item], known_refs=refs(item))
    local = [EvidenceItem(f"ev-{name}", name, "a" * 64, EvidenceStatus.PASS)
             for name in ("ci_jobs", "migrations", "sbom", "synthetic_e2e", "postgres_live",
                          "accessibility_contract", "capacity_simulation")]
    candidate = ReleaseCandidateEvaluator().evaluate(version="0.1.0-rc.1", commit_sha="abc",
        traceability=trace, evidence=local, now=NOW)
    assert candidate.verdict is ReleaseVerdict.BLOCKED
    for blocker in ("partner_official_api", "map_provider_sandbox", "legal_counsel_approval",
                    "operator_approval", "main_merge", "field_pilot"):
        assert blocker in candidate.blockers
    assert candidate.compliance_claim == "NOT_A_COMPLIANCE_OR_CERTIFICATION_CLAIM"


def test_expired_or_digestless_evidence_is_blocked():
    item = requirement()
    trace = TraceabilityVerifier.verify([item], known_refs=refs(item))
    evidence = [EvidenceItem("x", name, "a" * 64, EvidenceStatus.PASS)
                for name in ReleaseCandidateEvaluator.REQUIRED_EVIDENCE]
    evidence[0] = EvidenceItem("expired", evidence[0].category, "a" * 64, EvidenceStatus.PASS,
                               NOW - timedelta(seconds=1))
    candidate = ReleaseCandidateEvaluator().evaluate(version="0.1.0-rc.1", commit_sha="abc",
        traceability=trace, evidence=evidence, now=NOW)
    assert candidate.verdict is ReleaseVerdict.BLOCKED


def test_complete_evidence_only_reaches_operator_review_not_deployment():
    item = requirement()
    trace = TraceabilityVerifier.verify([item], known_refs=refs(item))
    evidence = [EvidenceItem("x", name, "a" * 64, EvidenceStatus.PASS)
                for name in ReleaseCandidateEvaluator.REQUIRED_EVIDENCE]
    candidate = ReleaseCandidateEvaluator().evaluate(version="0.1.0-rc.1", commit_sha="abc",
        traceability=trace, evidence=evidence, now=NOW)
    assert candidate.verdict is ReleaseVerdict.READY_FOR_OPERATOR_REVIEW
    assert "DEPLOY" not in candidate.verdict.value


def test_freeze_exception_requires_reason_risk_rollback_and_dual_approval():
    policy = ChangeFreezePolicy("0.1.0-rc.1", "abc")
    exception = FreezeException("fx-1", "critical safety correction", "risk://review/1",
        "rollback://plan/1", "ethernian:review-1", "operator:choi-inseok",
        NOW + timedelta(days=1))
    assert policy.authorize(["src/narang_rider/safety.py"], exception=exception, now=NOW)
    invalid = FreezeException("fx-2", "", "risk://review/1", "rollback://plan/1",
        "ethernian:review-1", "operator:choi-inseok", NOW + timedelta(days=1))
    assert not policy.authorize(["src/narang_rider/safety.py"], exception=invalid, now=NOW)


def test_expired_exception_fails_and_docs_only_change_is_allowed():
    policy = ChangeFreezePolicy("0.1.0-rc.1", "abc")
    expired = FreezeException("fx", "reason", "risk://1", "rollback://1",
        "ethernian:a", "operator:b", NOW - timedelta(seconds=1))
    assert not policy.authorize(["src/code.py"], exception=expired, now=NOW)
    assert policy.authorize(["docs/known-limitations.md"], exception=None, now=NOW)


def test_final_smoke_covers_operational_handoff_boundaries():
    assert len(FINAL_SMOKE_CHECKLIST) >= 10
    assert "rollback_rehearsal" in FINAL_SMOKE_CHECKLIST
    assert "privacy_rights_and_support_appeal" in FINAL_SMOKE_CHECKLIST
