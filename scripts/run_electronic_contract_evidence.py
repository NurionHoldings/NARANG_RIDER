"""Generate non-activating synthetic electronic partner-contract evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from narang_rider.electronic_contract import (
    ContractMandate,
    ContractParty,
    ContractTemplate,
    ElectronicContractService,
    RepresentativeVerification,
    SignatureEnvelope,
    VerifiedPaymentReceipt,
)

ROOT = Path(__file__).resolve().parents[1]


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def main() -> None:
    raw = json.loads((ROOT / "config/mobility-plaza-contract-template.json").read_text())
    if raw["status"] != "LEGAL_REVIEW_REQUIRED" or raw["production_approved"]:
        raise SystemExit("contract template must remain non-production and pending legal review")
    now = datetime(2026, 9, 16, tzinfo=UTC)
    template = ContractTemplate(
        raw["template_id"], raw["version"], tuple(sorted(raw["clauses"].items())),
        frozenset(raw["variable_allowlist"]), "legal:synthetic-template-review",
        "operator:synthetic-template-approval", now - timedelta(days=1), now + timedelta(days=30),
    )
    company = ContractParty("company", "나랑라이더 합성 운영법인", "registry:synthetic-company", "company-signer", digest("company-authority"))
    partner = ContractParty("partner", "합성 입점업체", "registry:synthetic-partner", "partner-signer", digest("partner-authority"))
    service = ElectronicContractService()
    mandate = ContractMandate(
        "synthetic-mandate", template.digest, partner.party_id, "maintenance", 0, 0,
        digest("fee-policy"), digest("eligibility"), digest("refund-policy"),
        digest("prepayment-disclosures"), "operator:fee-policy", now - timedelta(minutes=1),
        now + timedelta(days=30),
    )
    payment = VerifiedPaymentReceipt(
        "synthetic-payment", partner.party_id, mandate.mandate_id, 0, 0, "CARD", "PAID", now,
        "https://payment.synthetic.invalid/callback", digest("payment-receipt"), True,
    )
    subject_hash = sha256(partner.authorized_signer_id.encode()).hexdigest()
    representative = RepresentativeVerification(
        "synthetic-identity", partner.party_id, subject_hash, subject_hash, now,
        now + timedelta(days=1), "https://identity.synthetic.invalid/verify",
        digest("identity-receipt"), True,
    )
    contract = service.prepare_under_mandate(
        contract_id="synthetic-contract-1", template=template, company=company, partner=partner,
        variables={
            "company_name": company.legal_name,
            "partner_name": partner.legal_name,
            "vertical": "maintenance",
            "fee_terms": "합성 수수료 0원",
            "effective_date": "2026-09-16",
            "expiry_date": "2026-10-16",
        },
        created_at=now, expires_at=now + timedelta(days=30),
        mandate=mandate, payment=payment,
    )
    service.legal_review(contract.contract_id, "legal:synthetic-contract", now)
    service.operator_approve(contract.contract_id, "operator:synthetic-contract", now)
    service.present(
        contract.contract_id, recipient_party_id=partner.party_id, channel="SYNTHETIC_PORTAL",
        disclosures=("전자문서", "서명권한", "수수료·후원", "개인정보", "ARKAON 권한제한"), now=now,
    )
    service.accept_partner_signature_and_auto_execute(
        SignatureEnvelope(
            "synthetic-envelope", contract.contract_id, contract.document_digest,
            partner.party_id, partner.authorized_signer_id, partner.authority_evidence_digest,
            now, "synthetic-nonce", "https://signature.synthetic.invalid/envelopes",
            digest("signature-receipt"),
        ),
        mandate=mandate,
        representative=representative,
        now=now,
    )
    bundle = service.evidence_bundle(contract.contract_id)
    bundle["template_source_digest"] = digest(json.dumps(raw, ensure_ascii=False, sort_keys=True))
    output = ROOT / "build/electronic-contract-evidence.json"
    output.parent.mkdir(exist_ok=True)
    payload = json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output.write_text(payload)
    (output.with_suffix(".json.sha256")).write_text(digest(payload) + "\n")


if __name__ == "__main__":
    main()
