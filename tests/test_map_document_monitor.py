from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from narang_rider.map_document_monitor import (
    ChangeSeverity,
    DocumentMonitorRejected,
    KnowledgeState,
    MapDocumentMonitor,
    OfficialDocumentSnapshot,
    safe_refresh_proposal,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def snapshot(**changes):
    values = {
        "evidence_id": "official-v1",
        "provider_id": "synthetic",
        "official_url": "https://official.example/maps/docs",
        "title": "Official map documentation",
        "accessed_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=30),
        "content_sha256": sha256(b"v1").hexdigest(),
        "extracted_fields": frozenset({"attribution", "terms"}),
        "source_authority_verified": True,
    }
    values.update(changes)
    return OfficialDocumentSnapshot(**values)


def test_unchanged_current_snapshot_is_not_promoted_or_written():
    old = snapshot()
    new = snapshot(evidence_id="official-v2", accessed_at=NOW)
    report = MapDocumentMonitor().compare(previous=old, candidate=new, now=NOW)
    assert report.severity is ChangeSeverity.NONE
    assert report.knowledge_state is KnowledgeState.CURRENT
    assert not report.production_activation_allowed
    proposal = safe_refresh_proposal(report)
    assert proposal["next_action"] == "no_change"
    assert not proposal["registry_write_allowed"]
    assert not proposal["model_learning_allowed"]


def test_routine_content_change_requires_human_review_and_contract_tests():
    report = MapDocumentMonitor().compare(
        previous=snapshot(),
        candidate=snapshot(
            evidence_id="official-v2",
            accessed_at=NOW,
            content_sha256=sha256(b"v2").hexdigest(),
        ),
        now=NOW,
    )
    assert report.severity is ChangeSeverity.ROUTINE
    assert report.knowledge_state is KnowledgeState.HUMAN_REVIEW
    assert report.requires_contract_tests and report.requires_ethernian_review


@pytest.mark.parametrize("field", ["scheme", "query_parameters", "motorcycle_support"])
def test_sensitive_field_change_is_breaking_and_never_activates(field):
    report = MapDocumentMonitor().compare(
        previous=snapshot(),
        candidate=snapshot(
            evidence_id="official-v2",
            accessed_at=NOW,
            content_sha256=sha256(field.encode()).hexdigest(),
            extracted_fields=frozenset({"attribution", "terms", field}),
        ),
        now=NOW,
    )
    assert report.severity is ChangeSeverity.BREAKING
    assert report.knowledge_state is KnowledgeState.HUMAN_REVIEW
    assert not report.production_activation_allowed


def test_stale_candidate_cannot_refresh_knowledge():
    report = MapDocumentMonitor().compare(
        previous=snapshot(),
        candidate=snapshot(
            evidence_id="stale-v2",
            accessed_at=NOW - timedelta(hours=1),
            expires_at=NOW - timedelta(seconds=1),
        ),
        now=NOW,
    )
    assert report.knowledge_state is KnowledgeState.STALE
    assert report.requires_ethernian_review


@pytest.mark.parametrize(
    "changes",
    [
        {"source_authority_verified": False},
        {"contains_prompt_injection": True},
    ],
)
def test_untrusted_or_malicious_candidate_is_blocked(changes):
    report = MapDocumentMonitor().compare(
        previous=snapshot(),
        candidate=snapshot(evidence_id="bad-v2", accessed_at=NOW, **changes),
        now=NOW,
    )
    assert report.severity is ChangeSeverity.SECURITY
    assert report.knowledge_state is KnowledgeState.BLOCKED


def test_invalid_source_digest_and_raw_retention_fail_closed():
    for changes in (
        {"official_url": "http://official.example/maps"},
        {"content_sha256": "not-a-digest"},
        {"raw_document_retained": True},
    ):
        with pytest.raises(DocumentMonitorRejected):
            MapDocumentMonitor().compare(
                previous=snapshot(), candidate=snapshot(**changes), now=NOW
            )


def test_cross_provider_and_chronology_regression_are_rejected():
    for candidate in (
        snapshot(provider_id="other"),
        snapshot(accessed_at=NOW - timedelta(days=2)),
    ):
        with pytest.raises(DocumentMonitorRejected):
            MapDocumentMonitor().compare(previous=snapshot(), candidate=candidate, now=NOW)


def test_ci_artifact_is_deterministic_and_schema_versioned():
    new = snapshot(evidence_id="official-v2", accessed_at=NOW)
    first = MapDocumentMonitor().compare(previous=snapshot(), candidate=new, now=NOW)
    second = MapDocumentMonitor().compare(previous=snapshot(), candidate=new, now=NOW)
    assert first.report_digest == second.report_digest
    assert first.as_ci_artifact()["schema"] == "narang.map-document-change.v1"
