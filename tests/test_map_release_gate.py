from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from narang_rider.map_release_gate import (
    REQUIRED_EXTERNAL_GATES,
    REQUIRED_INTERNAL_EVIDENCE,
    ExternalGate,
    GateRejected,
    GateVerdict,
    InternalEvidence,
    MapReleaseGate,
)

NOW = datetime(2026, 9, 16, 16, tzinfo=UTC)
COMMIT = "a" * 40


def evidence(evidence_type, **changes):
    values = {
        "evidence_type": evidence_type,
        "status": "pass_for_human_review",
        "digest": sha256(evidence_type.encode()).hexdigest(),
        "source_commit": COMMIT,
        "generated_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(days=30),
        "synthetic_only": True,
    }
    values.update(changes)
    return InternalEvidence(**values)


def internal(**per_type):
    return tuple(
        evidence(name, **per_type.get(name, {}))
        for name in sorted(REQUIRED_INTERNAL_EVIDENCE)
    )


def gate(gate_id, **changes):
    values = {
        "gate_id": gate_id,
        "status": "VERIFIED",
        "evidence_digest": sha256(gate_id.encode()).hexdigest(),
        "evidence_expires_at": NOW + timedelta(days=30),
        "ethernian_review_ref": f"evidence-ref:{gate_id}:review",
        "operator_approval_ref": f"evidence-ref:{gate_id}:operator",
    }
    values.update(changes)
    return ExternalGate(**values)


def external(**per_gate):
    return tuple(
        gate(name, **per_gate.get(name, {}))
        for name in sorted(REQUIRED_EXTERNAL_GATES)
    )


def evaluate(**changes):
    values = {
        "candidate_commit": COMMIT,
        "internal_evidence": internal(),
        "external_gates": external(),
        "ethernian_final_review_ref": "evidence-ref:ethernian:final",
        "operator_decision_ref": "evidence-ref:operator:decision",
        "now": NOW,
    }
    values.update(changes)
    return MapReleaseGate().evaluate(**values)


def test_complete_evidence_only_reaches_operator_decision_not_activation():
    report = evaluate()
    assert report.verdict is GateVerdict.READY_FOR_OPERATOR_DECISION
    assert report.blockers == ()
    assert report.operator_decision_required
    assert not report.main_merge_allowed
    assert not report.deployment_allowed
    assert not report.production_activation_allowed
    assert report.as_ci_artifact()["schema"] == "narang.map-release-gate.v1"


@pytest.mark.parametrize("missing", sorted(REQUIRED_INTERNAL_EVIDENCE))
def test_each_internal_evidence_type_is_mandatory(missing):
    items = tuple(item for item in internal() if item.evidence_type != missing)
    report = evaluate(internal_evidence=items)
    assert report.verdict is GateVerdict.BLOCKED
    assert f"internal_missing:{missing}" in report.blockers


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "blocked"},
        {"digest": "bad"},
        {"source_commit": "b" * 40},
        {"generated_at": NOW + timedelta(seconds=1)},
        {"expires_at": NOW},
        {"synthetic_only": False},
    ],
)
def test_invalid_internal_evidence_is_blocked(changes):
    items = internal(document_monitor=changes)
    report = evaluate(internal_evidence=items)
    assert "internal_invalid:document_monitor" in report.blockers


@pytest.mark.parametrize("missing", sorted(REQUIRED_EXTERNAL_GATES))
def test_each_external_gate_is_mandatory(missing):
    items = tuple(item for item in external() if item.gate_id != missing)
    report = evaluate(external_gates=items)
    assert report.verdict is GateVerdict.BLOCKED
    assert f"external_missing:{missing}" in report.blockers


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "PENDING"},
        {"evidence_digest": None},
        {"evidence_digest": "bad"},
        {"evidence_expires_at": NOW},
        {"ethernian_review_ref": None},
    ],
)
def test_pending_stale_or_incomplete_external_gate_is_blocked(changes):
    report = evaluate(external_gates=external(**{"EXT-02": changes}))
    assert "external_not_verified:EXT-02" in report.blockers


def test_current_real_project_pending_gates_are_explicitly_blocked():
    pending = tuple(
        gate(name, status="PENDING", evidence_digest=None, evidence_expires_at=None)
        for name in sorted(REQUIRED_EXTERNAL_GATES)
    )
    report = evaluate(
        external_gates=pending,
        ethernian_final_review_ref=None,
        operator_decision_ref=None,
    )
    assert report.verdict is GateVerdict.BLOCKED
    assert len(report.blockers) == len(REQUIRED_EXTERNAL_GATES) + 2


def test_independent_review_and_operator_decision_cannot_be_same_reference():
    report = evaluate(
        ethernian_final_review_ref="evidence-ref:same",
        operator_decision_ref="evidence-ref:same",
    )
    assert "independent_approval_separation_failed" in report.blockers


def test_missing_final_reviews_are_blocked():
    report = evaluate(ethernian_final_review_ref=None, operator_decision_ref=None)
    assert "ethernian_final_review_missing" in report.blockers
    assert "operator_decision_missing" in report.blockers


def test_duplicate_evidence_and_external_gates_are_rejected():
    with pytest.raises(GateRejected, match="duplicate internal"):
        evaluate(internal_evidence=internal() + (evidence("document_monitor"),))
    with pytest.raises(GateRejected, match="duplicate external"):
        evaluate(external_gates=external() + (gate("EXT-02"),))


@pytest.mark.parametrize("commit", ["short", "A" * 40, "z" * 40])
def test_candidate_commit_must_be_full_lowercase_sha(commit):
    with pytest.raises(GateRejected):
        evaluate(candidate_commit=commit)


def test_report_digest_is_deterministic_and_contains_only_references():
    first = evaluate()
    second = evaluate()
    assert first.report_digest == second.report_digest
    serialized = repr(first)
    assert "api_key" not in serialized
    assert "latitude" not in serialized
    assert "rider_id" not in serialized
