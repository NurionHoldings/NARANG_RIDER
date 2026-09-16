from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import pairwise

import pytest

from narang_rider.electronic_contract import (
    REQUIRED_CLAUSES,
    ContractParty,
    ContractRejected,
    ContractStage,
    ContractTemplate,
    ElectronicContractService,
    SignatureEnvelope,
)

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def template():
    variables = frozenset({"company_name", "partner_name"})
    clauses = tuple(
        (clause, f"{clause}: {{{{company_name}}}} / {{{{partner_name}}}}")
        for clause in sorted(REQUIRED_CLAUSES)
    )
    return ContractTemplate(
        "template-1", "1.0.0", clauses, variables, "legal:template:1", "operator:template:1",
        NOW - timedelta(days=1), NOW + timedelta(days=90),
    )


def party(party_id: str, signer_id: str):
    return ContractParty(party_id, f"법인-{party_id}", f"registry:{party_id}", signer_id, digest(f"authority:{signer_id}"))


def prepared(service=None, contract_id="contract-1", supersedes=None):
    service = service or ElectronicContractService()
    contract = service.prepare(
        contract_id=contract_id,
        template=template(),
        company=party("company", "company-signer"),
        partner=party("partner", "partner-signer"),
        variables={"company_name": "나랑라이더 운영법인", "partner_name": "합성 입점사"},
        created_at=NOW,
        expires_at=NOW + timedelta(days=30),
        supersedes_contract_id=supersedes,
    )
    return service, contract


def approved_and_presented(service=None, contract_id="contract-1"):
    service, contract = prepared(service, contract_id)
    service.legal_review(contract.contract_id, "legal:contract:1", NOW)
    service.operator_approve(contract.contract_id, "operator:contract:1", NOW)
    receipt = service.present(
        contract.contract_id,
        recipient_party_id="partner",
        channel="SYNTHETIC_PORTAL",
        disclosures=("전자문서", "서명권한", "수수료·후원", "개인정보", "ARKAON 권한제한"),
        now=NOW,
    )
    return service, service.get(contract.contract_id), receipt


def envelope(contract, party_id, signer_id, marker):
    return SignatureEnvelope(
        f"envelope-{marker}", contract.contract_id, contract.document_digest, party_id, signer_id,
        digest(f"authority:{signer_id}"), NOW, f"nonce-{marker}",
        "https://signature.synthetic.invalid/envelopes", digest(f"receipt-{marker}"),
    )


def execute_contract():
    service, contract, _ = approved_and_presented()
    service.accept_partner_signature(envelope(contract, "partner", "partner-signer", "partner"), NOW)
    service.accept_company_signature(envelope(contract, "company", "company-signer", "company"), NOW)
    executed = service.execute(contract.contract_id, "operator:execute:1", NOW)
    return service, executed


def test_arkaon_prepares_only_exact_approved_template_and_variables():
    service, contract = prepared()
    assert contract.stage is ContractStage.ARKAON_PREPARED
    assert contract.template_digest == template().digest
    assert not contract.arkaon_signed and not contract.real_signature_allowed
    with pytest.raises(ContractRejected, match="exact"):
        service.prepare(
            contract_id="bad", template=template(), company=party("c", "s1"), partner=party("p", "s2"),
            variables={"company_name": "c", "partner_name": "p", "invented": "clause"},
            created_at=NOW, expires_at=NOW + timedelta(days=1),
        )


def test_legal_operator_and_signer_roles_are_separated():
    service, contract = prepared()
    service.legal_review(contract.contract_id, "same", NOW)
    with pytest.raises(ContractRejected, match="separate"):
        service.operator_approve(contract.contract_id, "same", NOW)


def test_complete_disclosures_are_required_before_presentation():
    service, contract = prepared()
    service.legal_review(contract.contract_id, "legal", NOW)
    service.operator_approve(contract.contract_id, "operator", NOW)
    with pytest.raises(ContractRejected, match="disclosures"):
        service.present(
            contract.contract_id, recipient_party_id="partner", channel="SYNTHETIC_PORTAL",
            disclosures=("전자문서",), now=NOW,
        )


def test_partner_and_company_sign_exact_same_document_then_operator_executes():
    service, contract, receipt = approved_and_presented()
    assert receipt.document_digest == contract.document_digest
    service.accept_partner_signature(envelope(contract, "partner", "partner-signer", "partner"), NOW)
    company_signed = service.accept_company_signature(
        envelope(contract, "company", "company-signer", "company"), NOW
    )
    assert company_signed.stage is ContractStage.COMPANY_SIGNED
    executed = service.execute(contract.contract_id, "operator:execute:1", NOW)
    assert executed.stage is ContractStage.EXECUTED
    bundle = service.evidence_bundle(contract.contract_id)
    assert bundle["arkaon_is_contracting_party"] is False
    assert bundle["arkaon_signature_allowed"] is False
    assert bundle["production_execution_allowed"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"party_id": "attacker"},
        {"signer_id": "wrong"},
        {"document_digest": digest("tampered")},
        {"signer_authority_digest": digest("wrong-authority")},
    ],
)
def test_signature_identity_authority_and_document_tampering_are_rejected(change):
    service, contract, _ = approved_and_presented()
    valid = envelope(contract, "partner", "partner-signer", "partner")
    values = {**valid.__dict__, **change}
    with pytest.raises(ContractRejected, match="mismatch"):
        service.accept_partner_signature(SignatureEnvelope(**values), NOW)


def test_signature_provider_must_be_synthetic_and_replay_is_rejected():
    service, contract, _ = approved_and_presented()
    valid = envelope(contract, "partner", "partner-signer", "partner")
    service.accept_partner_signature(valid, NOW)
    with pytest.raises(ContractRejected):
        service._verify_envelope(valid, contract, contract.partner, NOW)
    with pytest.raises(ContractRejected, match="synthetic"):
        SignatureEnvelope(**{**valid.__dict__, "provider_endpoint": "https://real-sign.example/api"})


def test_expired_unsigned_contract_is_closed_by_arkaon_monitor_not_signed():
    service, contract = prepared()
    expired = service.expire_due(contract.contract_id, NOW + timedelta(days=31))
    assert expired.stage is ContractStage.EXPIRED
    assert service.audit[-1].actor_type == "ARKAON"
    assert service.audit[-1].action == "EXPIRED"


def test_executed_contract_is_immutable_and_amendment_creates_new_lineage():
    service, executed = execute_contract()
    with pytest.raises(ContractRejected, match="immutable"):
        service.withdraw(executed.contract_id, "operator", digest("reason"), NOW)
    _, amendment = prepared(service, "contract-2", supersedes=executed.contract_id)
    assert amendment.supersedes_contract_id == executed.contract_id
    assert amendment.document_digest != executed.document_digest


def test_archive_and_audit_hash_chain_are_deterministic_and_complete():
    service, executed = execute_contract()
    archived = service.archive(executed.contract_id, digest("archive"), NOW + timedelta(days=31))
    assert archived.stage is ContractStage.ARCHIVED
    events = [event for event in service.audit if event.contract_id == executed.contract_id]
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    for previous, current in pairwise(events):
        assert current.previous_event_digest == previous.event_digest
    assert service.evidence_bundle(executed.contract_id)["event_chain_head"] == events[-1].event_digest
