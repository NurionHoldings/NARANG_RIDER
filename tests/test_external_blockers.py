from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from narang_rider.external_readiness import build_operator_dashboard, external_readiness_api
from scripts.validate_external_blockers import (
    BlockerValidationError,
    load_registry,
    validate_registry,
)

REGISTRY=Path("config/external-blockers.json")

def test_registry_is_valid_and_release_blocked():
    data=load_registry(REGISTRY); validate_registry(data,now=datetime(2026,9,16,tzinfo=UTC))
    assert data["release_gate"]=="BLOCKED"
    assert [b["status"] for b in data["blockers"]]==["PENDING"]*7

def test_verified_requires_digest_reviews_and_operator():
    data=load_registry(REGISTRY); item=data["blockers"][0]; item["status"]="VERIFIED"
    with pytest.raises(BlockerValidationError,match="complete evidence"): validate_registry(data)

def test_dependency_order_is_fail_closed():
    data=load_registry(REGISTRY); data["blockers"][0]["dependencies"]=["EXT-07"]
    with pytest.raises(BlockerValidationError,match="invalid dependency"): validate_registry(data)

def test_expired_evidence_is_rejected():
    data=load_registry(REGISTRY); item=data["blockers"][0]
    item.update(status="VERIFIED",evidence_refs=["evidence-ref:ext01/report"],evidence_digest="sha256:"+"a"*64,evidence_generated_at="2026-01-01T00:00:00Z",evidence_expires_at="2026-02-01T00:00:00Z",ethernian_review_ref="evidence-ref:review/ext01",operator_approval_ref="evidence-ref:operator/ext01")
    with pytest.raises(BlockerValidationError,match="expired"): validate_registry(data,now=datetime(2026,9,16,tzinfo=UTC))

def test_sensitive_fields_are_rejected():
    data=load_registry(REGISTRY); data["blockers"][0]["api_key"]="do-not-store"
    with pytest.raises(BlockerValidationError,match="sensitive field"): validate_registry(data)

def test_operator_dashboard_is_read_only_and_actionable():
    view=build_operator_dashboard(REGISTRY); payload=external_readiness_api(REGISTRY)
    assert view.next_action_ids==("EXT-01","EXT-02")
    assert payload["release_gate"]=="BLOCKED"
    assert "자동 수행하지 않습니다" in payload["notice"]
