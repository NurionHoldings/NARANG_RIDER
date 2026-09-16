from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import pairwise

import pytest

from narang_rider.contract_lifecycle import (
    ContractLifecycleService,
    EntitlementMandate,
    LifecycleRejected,
    LifecycleSignal,
    LifecycleStage,
    Recommendation,
)
from narang_rider.electronic_contract import ContractParty, ContractStage, ElectronicContract

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def party(party_id: str) -> ContractParty:
    return ContractParty(
        party_id,
        f"법인-{party_id}",
        f"registry:{party_id}",
        f"signer:{party_id}",
        digest(f"authority:{party_id}"),
    )


def contract(contract_id: str = "contract-1", *, partner_id: str = "partner") -> ElectronicContract:
    return ElectronicContract(
        contract_id=contract_id,
        template_id="template-1",
        template_version="1",
        template_digest=digest("template"),
        company=party("company"),
        partner=party(partner_id),
        variables=(("partner", partner_id),),
        rendered_clauses=(("scope", "synthetic"),),
        document_digest=digest(f"document:{contract_id}"),
        created_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=365),
        stage=ContractStage.AUTO_EXECUTED,
        execution_digest=digest(f"execution:{contract_id}"),
    )


def mandate(value: ElectronicContract) -> EntitlementMandate:
    return EntitlementMandate(
        mandate_id=f"mandate:{value.contract_id}",
        contract_id=value.contract_id,
        contract_execution_digest=value.execution_digest or "",
        partner_party_id=value.partner.party_id,
        vertical="MOTORCYCLE_MARKET",
        entitlements=frozenset({"LISTING_WRITE", "CONTRACT_READ", "SUPPORT_REQUEST"}),
        approved_by="operator:entitlements:1",
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(days=30),
    )


def active(service: ContractLifecycleService | None = None):
    service = service or ContractLifecycleService()
    value = contract()
    lifecycle = service.activate(
        lifecycle_id="lifecycle-1",
        contract=value,
        mandate=mandate(value),
        starts_at=NOW,
        ends_at=NOW + timedelta(days=180),
    )
    return service, lifecycle


def signal(kind: str, marker: str = "1", partner_id: str = "partner") -> LifecycleSignal:
    return LifecycleSignal(
        f"signal-{marker}", partner_id, kind, NOW + timedelta(days=1), digest(f"signal:{marker}")
    )


def test_exact_preapproved_entitlements_activate_after_executed_contract_only():
    service, lifecycle = active()
    assert lifecycle.stage is LifecycleStage.ACTIVE
    assert lifecycle.entitlements == mandate(contract()).entitlements
    assert service.audit[-1].actor_type == "PREAPPROVED_POLICY_ENGINE"
    unexecuted = replace(contract("draft"), stage=ContractStage.PRESENTED, execution_digest=None)
    with pytest.raises(LifecycleRejected, match="executed"):
        service.activate(
            lifecycle_id="bad", contract=unexecuted, mandate=mandate(contract("draft")),
            starts_at=NOW, ends_at=NOW + timedelta(days=1),
        )


def test_contract_partner_execution_and_expiry_mismatch_fail_closed():
    service = ContractLifecycleService()
    value = contract()
    bad = replace(mandate(value), partner_party_id="attacker")
    with pytest.raises(LifecycleRejected, match="mismatch"):
        service.activate(
            lifecycle_id="bad", contract=value, mandate=bad,
            starts_at=NOW, ends_at=NOW + timedelta(days=1),
        )
    with pytest.raises(LifecycleRejected, match="mismatch"):
        service.activate(
            lifecycle_id="too-long", contract=value, mandate=mandate(value),
            starts_at=NOW, ends_at=value.expires_at + timedelta(days=1),
        )


@pytest.mark.parametrize(
    ("kind", "recommendation"),
    [
        ("RENEWAL_WINDOW", Recommendation.REVIEW_RENEWAL),
        ("COMPLIANCE_STALE", Recommendation.REVIEW_COMPLIANCE),
        ("PAYMENT_OVERDUE", Recommendation.REVIEW_PAYMENT),
    ],
)
def test_arkaon_monitoring_only_recommends_human_review(kind, recommendation):
    service, lifecycle = active()
    reviewed = service.observe(lifecycle.lifecycle_id, signal(kind))
    assert reviewed.stage is LifecycleStage.HUMAN_REVIEW
    assert reviewed.recommendation is recommendation
    assert service.audit[-1].actor_type == "ARKAON"
    assert service.evidence_bundle(lifecycle.lifecycle_id)["arkaon_can_suspend_or_terminate"] is False


def test_cross_partner_and_replayed_monitoring_signals_are_rejected():
    service, lifecycle = active()
    with pytest.raises(LifecycleRejected, match="cross-partner"):
        service.observe(lifecycle.lifecycle_id, signal("PAYMENT_OVERDUE", partner_id="other"))
    value = signal("PAYMENT_OVERDUE")
    service.observe(lifecycle.lifecycle_id, value)
    with pytest.raises(LifecycleRejected):
        service.observe(lifecycle.lifecycle_id, value)


def test_monitoring_after_contract_window_is_rejected():
    service, lifecycle = active()
    late = replace(
        signal("COMPLIANCE_STALE"),
        observed_at=lifecycle.ends_at,
    )
    with pytest.raises(LifecycleRejected, match="outside"):
        service.observe(lifecycle.lifecycle_id, late)


def test_operator_can_continue_or_suspend_but_arkaon_cannot_decide():
    service, lifecycle = active()
    service.observe(lifecycle.lifecycle_id, signal("COMPLIANCE_STALE"))
    continued = service.continue_after_review(
        lifecycle.lifecycle_id,
        decision_ref="operator:review:1",
        decision_digest=digest("continue"),
        now=NOW + timedelta(days=2),
    )
    assert continued.stage is LifecycleStage.ACTIVE
    suspended = service.suspend(
        lifecycle.lifecycle_id,
        decision_ref="operator:suspend:1",
        decision_digest=digest("suspend"),
        now=NOW + timedelta(days=3),
    )
    assert suspended.stage is LifecycleStage.SUSPENDED
    for action in ("ACTIVATE", "CHANGE_ENTITLEMENTS", "SUSPEND", "TERMINATE", "RENEW"):
        with pytest.raises(LifecycleRejected, match="authority"):
            service.arkaon_action(action)


def test_termination_requires_independent_confirmation():
    service, lifecycle = active()
    pending = service.request_termination(
        lifecycle.lifecycle_id,
        requester_ref="operator:a",
        decision_digest=digest("termination-request"),
        now=NOW + timedelta(days=1),
    )
    assert pending.stage is LifecycleStage.TERMINATION_PENDING
    with pytest.raises(LifecycleRejected, match="independent"):
        service.confirm_termination(
            lifecycle.lifecycle_id,
            confirmer_ref="operator:a",
            confirmation_digest=digest("confirmation"),
            now=NOW + timedelta(days=2),
        )
    terminated = service.confirm_termination(
        lifecycle.lifecycle_id,
        confirmer_ref="operator:b",
        confirmation_digest=digest("confirmation"),
        now=NOW + timedelta(days=2),
    )
    assert terminated.stage is LifecycleStage.TERMINATED


def test_amendment_requires_executed_same_partner_superseding_contract():
    service, lifecycle = active()
    amendment = replace(
        contract("contract-2"),
        supersedes_contract_id=lifecycle.contract_id,
    )
    linked = service.link_superseding_contract(
        lifecycle.lifecycle_id,
        new_contract=amendment,
        operator_ref="operator:amendment:1",
        now=NOW + timedelta(days=1),
    )
    assert linked.superseded_by_contract_id == "contract-2"
    with pytest.raises(LifecycleRejected, match="same-partner"):
        service.link_superseding_contract(
            lifecycle.lifecycle_id,
            new_contract=replace(amendment, partner=party("other")),
            operator_ref="operator:amendment:1",
            now=NOW + timedelta(days=1),
        )


def test_expiry_and_append_only_evidence_chain_are_deterministic():
    service, lifecycle = active()
    expired = service.expire_due(lifecycle.lifecycle_id, NOW + timedelta(days=181))
    assert expired.stage is LifecycleStage.EXPIRED
    events = service.audit
    for previous, current in pairwise(events):
        assert current.previous_event_digest == previous.event_digest
    bundle = service.evidence_bundle(lifecycle.lifecycle_id)
    assert bundle["event_chain_head"] == events[-1].event_digest
    assert bundle["production_activation_allowed"] is False
