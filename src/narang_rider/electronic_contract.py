"""ARKAON-managed, human-authorized electronic partner contracting workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit


class ContractRejected(ValueError):
    pass


class ContractStage(StrEnum):
    DRAFT = "draft"
    ARKAON_PREPARED = "arkaon_prepared"
    LEGAL_REVIEWED = "legal_reviewed"
    OPERATOR_APPROVED = "operator_approved"
    PRESENTED = "presented"
    PARTNER_SIGNED = "partner_signed"
    COMPANY_SIGNED = "company_signed"
    EXECUTED = "executed"
    AUTO_EXECUTED = "auto_executed"
    ARCHIVED = "archived"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"


REQUIRED_CLAUSES = frozenset(
    {
        "parties_and_authority",
        "service_scope",
        "fees_and_sponsorship",
        "consumer_and_financial_boundary",
        "personal_data_boundary",
        "security_and_incident",
        "monitoring_and_audit",
        "term_renewal_termination",
        "dispute_and_notice",
        "electronic_document_and_signature",
        "arkaon_authority_limit",
    }
)


def _digest_ok(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def canonical_digest(value: Any) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ContractTemplate:
    template_id: str
    version: str
    clauses: tuple[tuple[str, str], ...]
    variable_allowlist: frozenset[str]
    legal_review_ref: str
    operator_approval_ref: str
    effective_at: datetime
    expires_at: datetime
    production_approved: bool = False

    def __post_init__(self) -> None:
        clause_ids = {clause_id for clause_id, _ in self.clauses}
        if not self.template_id or not self.version or clause_ids != REQUIRED_CLAUSES:
            raise ContractRejected("complete approved clause set required")
        if any(not text.strip() for _, text in self.clauses):
            raise ContractRejected("empty contract clause forbidden")
        if not self.legal_review_ref or not self.operator_approval_ref:
            raise ContractRejected("legal and operator template approvals required")
        if self.legal_review_ref == self.operator_approval_ref:
            raise ContractRejected("template approval separation required")
        if self.effective_at.tzinfo is None or not self.effective_at < self.expires_at:
            raise ContractRejected("bounded timezone-aware template validity required")
        if self.production_approved:
            raise ContractRejected("synthetic template cannot assert production approval")

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "template_id": self.template_id,
                "version": self.version,
                "clauses": self.clauses,
                "variables": sorted(self.variable_allowlist),
                "legal_review_ref": self.legal_review_ref,
                "operator_approval_ref": self.operator_approval_ref,
                "effective_at": self.effective_at.isoformat(),
                "expires_at": self.expires_at.isoformat(),
            }
        )


@dataclass(frozen=True)
class ContractParty:
    party_id: str
    legal_name: str
    registration_ref: str
    authorized_signer_id: str
    authority_evidence_digest: str

    def __post_init__(self) -> None:
        if not all(
            item.strip()
            for item in (
                self.party_id,
                self.legal_name,
                self.registration_ref,
                self.authorized_signer_id,
            )
        ) or not _digest_ok(self.authority_evidence_digest):
            raise ContractRejected("verified legal party and signer authority required")


@dataclass(frozen=True)
class ContractMandate:
    mandate_id: str
    template_digest: str
    partner_party_id: str
    vertical: str
    onboarding_fee_won: int
    usage_fee_won: int
    fee_policy_digest: str
    eligibility_evidence_digest: str
    refund_policy_digest: str
    prepayment_disclosure_receipt_digest: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    delegated_actions: frozenset[str] = frozenset({"PREPARE", "PRESENT", "AUTO_EXECUTE"})

    def __post_init__(self) -> None:
        if (
            not self.mandate_id
            or not self.partner_party_id
            or not self.vertical
            or not self.approved_by
            or not _digest_ok(self.template_digest)
            or not _digest_ok(self.fee_policy_digest)
            or not _digest_ok(self.eligibility_evidence_digest)
            or not _digest_ok(self.refund_policy_digest)
            or not _digest_ok(self.prepayment_disclosure_receipt_digest)
            or self.onboarding_fee_won < 0
            or self.usage_fee_won < 0
            or self.approved_at.tzinfo is None
            or not self.approved_at < self.expires_at
        ):
            raise ContractRejected("complete bounded operator mandate required")
        if self.delegated_actions != frozenset({"PREPARE", "PRESENT", "AUTO_EXECUTE"}):
            raise ContractRejected("exact non-discretionary ARKAON mandate required")

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                **self.__dict__,
                "approved_at": self.approved_at.isoformat(),
                "expires_at": self.expires_at.isoformat(),
                "delegated_actions": sorted(self.delegated_actions),
            }
        )


@dataclass(frozen=True)
class VerifiedPaymentReceipt:
    receipt_id: str
    partner_party_id: str
    mandate_id: str
    onboarding_fee_won: int
    usage_fee_won: int
    method: str
    status: str
    paid_at: datetime
    provider_endpoint: str
    provider_receipt_digest: str
    callback_verified: bool
    synthetic: bool = True

    def __post_init__(self) -> None:
        parsed = urlsplit(self.provider_endpoint)
        if (
            not self.receipt_id
            or self.method not in {"BANK_TRANSFER", "CARD"}
            or self.status != "PAID"
            or self.onboarding_fee_won < 0
            or self.usage_fee_won < 0
            or self.paid_at.tzinfo is None
            or not _digest_ok(self.provider_receipt_digest)
            or not self.callback_verified
        ):
            raise ContractRejected("verified completed payment receipt required")
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".invalid"):
            raise ContractRejected("synthetic payment provider required")
        if not self.synthetic:
            raise ContractRejected("real payment confirmation is forbidden")


@dataclass(frozen=True)
class RepresentativeVerification:
    verification_id: str
    partner_party_id: str
    registered_representative_subject_hash: str
    verified_identity_subject_hash: str
    verified_at: datetime
    expires_at: datetime
    provider_endpoint: str
    provider_receipt_digest: str
    verified: bool
    synthetic: bool = True

    def __post_init__(self) -> None:
        parsed = urlsplit(self.provider_endpoint)
        if (
            not self.verification_id
            or not _digest_ok(self.registered_representative_subject_hash)
            or not _digest_ok(self.verified_identity_subject_hash)
            or not _digest_ok(self.provider_receipt_digest)
            or self.registered_representative_subject_hash != self.verified_identity_subject_hash
            or self.verified_at.tzinfo is None
            or not self.verified_at < self.expires_at
            or not self.verified
        ):
            raise ContractRejected("business representative identity match required")
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".invalid"):
            raise ContractRejected("synthetic identity provider required")
        if not self.synthetic:
            raise ContractRejected("real identity data is forbidden")


@dataclass(frozen=True)
class ElectronicContract:
    contract_id: str
    template_id: str
    template_version: str
    template_digest: str
    company: ContractParty
    partner: ContractParty
    variables: tuple[tuple[str, str], ...]
    rendered_clauses: tuple[tuple[str, str], ...]
    document_digest: str
    created_at: datetime
    expires_at: datetime
    stage: ContractStage = ContractStage.DRAFT
    legal_review_ref: str | None = None
    operator_approval_ref: str | None = None
    presentation_receipt_digest: str | None = None
    partner_signature_digest: str | None = None
    company_signature_digest: str | None = None
    execution_digest: str | None = None
    archive_digest: str | None = None
    supersedes_contract_id: str | None = None
    mandate_digest: str | None = None
    payment_receipt_digest: str | None = None
    representative_verification_digest: str | None = None
    arkaon_signed: bool = False
    real_signature_allowed: bool = False

    def __post_init__(self) -> None:
        if self.company.party_id == self.partner.party_id:
            raise ContractRejected("distinct contracting parties required")
        if not _digest_ok(self.template_digest) or not _digest_ok(self.document_digest):
            raise ContractRejected("template and document digest required")
        if self.created_at.tzinfo is None or not self.created_at < self.expires_at:
            raise ContractRejected("bounded contract validity required")
        if self.arkaon_signed or self.real_signature_allowed:
            raise ContractRejected("ARKAON signature and real execution are forbidden")


@dataclass(frozen=True)
class PresentationReceipt:
    receipt_id: str
    contract_id: str
    document_digest: str
    recipient_party_id: str
    presented_at: datetime
    expires_at: datetime
    channel: str
    acknowledged_disclosures: tuple[str, ...]
    receipt_digest: str


@dataclass(frozen=True)
class SignatureEnvelope:
    envelope_id: str
    contract_id: str
    document_digest: str
    party_id: str
    signer_id: str
    signer_authority_digest: str
    signed_at: datetime
    nonce: str
    provider_endpoint: str
    provider_receipt_digest: str
    synthetic: bool = True

    def __post_init__(self) -> None:
        parsed = urlsplit(self.provider_endpoint)
        if (
            not self.envelope_id
            or not self.nonce
            or not _digest_ok(self.document_digest)
            or not _digest_ok(self.signer_authority_digest)
            or not _digest_ok(self.provider_receipt_digest)
            or self.signed_at.tzinfo is None
        ):
            raise ContractRejected("complete signed envelope evidence required")
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".invalid"):
            raise ContractRejected("synthetic signature provider required")
        if not self.synthetic:
            raise ContractRejected("real signature submission is forbidden")


@dataclass(frozen=True)
class ContractAuditEvent:
    sequence: int
    contract_id: str
    action: str
    actor_type: str
    actor_ref: str
    occurred_at: datetime
    evidence_digest: str
    previous_event_digest: str
    event_digest: str


class ElectronicContractService:
    """Lets ARKAON orchestrate evidence, while only authorized humans sign."""

    def __init__(self) -> None:
        self._contracts: dict[str, ElectronicContract] = {}
        self._used_envelope_ids: set[str] = set()
        self._used_nonces: set[str] = set()
        self.audit: list[ContractAuditEvent] = []

    def prepare_under_mandate(
        self,
        *,
        contract_id: str,
        template: ContractTemplate,
        company: ContractParty,
        partner: ContractParty,
        variables: dict[str, str],
        created_at: datetime,
        expires_at: datetime,
        mandate: ContractMandate,
        payment: VerifiedPaymentReceipt,
        supersedes_contract_id: str | None = None,
    ) -> ElectronicContract:
        if contract_id in self._contracts:
            raise ContractRejected("duplicate contract")
        if not template.effective_at <= created_at < template.expires_at:
            raise ContractRejected("template is not currently valid")
        if (
            mandate.template_digest != template.digest
            or mandate.partner_party_id != partner.party_id
            or created_at >= mandate.expires_at
            or payment.partner_party_id != partner.party_id
            or payment.mandate_id != mandate.mandate_id
            or payment.onboarding_fee_won != mandate.onboarding_fee_won
            or payment.usage_fee_won != mandate.usage_fee_won
            or payment.paid_at > created_at
        ):
            raise ContractRejected("mandate and payment prerequisites mismatch")
        if set(variables) != set(template.variable_allowlist):
            raise ContractRejected("exact approved template variables required")
        if any(not key.strip() or not value.strip() for key, value in variables.items()):
            raise ContractRejected("blank contract variable forbidden")
        if supersedes_contract_id is not None:
            prior = self.get(supersedes_contract_id)
            if prior.stage not in {
                ContractStage.EXECUTED,
                ContractStage.AUTO_EXECUTED,
                ContractStage.ARCHIVED,
            }:
                raise ContractRejected("only executed contract may be superseded")
        ordered_variables = tuple(sorted(variables.items()))
        rendered = tuple(
            (clause_id, self._render(text, variables)) for clause_id, text in template.clauses
        )
        document_digest = canonical_digest(
            {
                "contract_id": contract_id,
                "template_digest": template.digest,
                "company": company.party_id,
                "partner": partner.party_id,
                "variables": ordered_variables,
                "clauses": rendered,
                "supersedes": supersedes_contract_id,
            }
        )
        contract = ElectronicContract(
            contract_id,
            template.template_id,
            template.version,
            template.digest,
            company,
            partner,
            ordered_variables,
            rendered,
            document_digest,
            created_at,
            expires_at,
            ContractStage.ARKAON_PREPARED,
            supersedes_contract_id=supersedes_contract_id,
            mandate_digest=mandate.digest,
            payment_receipt_digest=payment.provider_receipt_digest,
        )
        self._contracts[contract_id] = contract
        self._record(contract, "ARKAON_PREPARED", "ARKAON", "system", created_at, document_digest)
        return contract

    def legal_review(self, contract_id: str, review_ref: str, now: datetime) -> ElectronicContract:
        contract = self._require(contract_id, ContractStage.ARKAON_PREPARED, now)
        if not review_ref.strip():
            raise ContractRejected("legal review reference required")
        updated = replace(contract, stage=ContractStage.LEGAL_REVIEWED, legal_review_ref=review_ref)
        return self._save(updated, "LEGAL_REVIEWED", "HUMAN_REVIEWER", review_ref, now, contract.document_digest)

    def operator_approve(self, contract_id: str, approval_ref: str, now: datetime) -> ElectronicContract:
        contract = self._require(contract_id, ContractStage.LEGAL_REVIEWED, now)
        if not approval_ref.strip() or approval_ref == contract.legal_review_ref:
            raise ContractRejected("separate operator approval required")
        updated = replace(contract, stage=ContractStage.OPERATOR_APPROVED, operator_approval_ref=approval_ref)
        return self._save(updated, "OPERATOR_APPROVED", "OPERATOR", approval_ref, now, contract.document_digest)

    def present(
        self,
        contract_id: str,
        *,
        recipient_party_id: str,
        channel: str,
        disclosures: tuple[str, ...],
        now: datetime,
    ) -> PresentationReceipt:
        contract = self._require(contract_id, ContractStage.OPERATOR_APPROVED, now)
        required = {"전자문서", "서명권한", "수수료·후원", "개인정보", "ARKAON 권한제한"}
        if recipient_party_id != contract.partner.party_id or set(disclosures) != required:
            raise ContractRejected("partner and complete disclosures required")
        if channel not in {"SYNTHETIC_PORTAL", "SYNTHETIC_EMAIL"}:
            raise ContractRejected("approved synthetic presentation channel required")
        receipt_id = f"presentation:{contract_id}"
        receipt_digest = canonical_digest(
            (receipt_id, contract.document_digest, recipient_party_id, channel, sorted(disclosures), now.isoformat())
        )
        receipt = PresentationReceipt(
            receipt_id,
            contract_id,
            contract.document_digest,
            recipient_party_id,
            now,
            contract.expires_at,
            channel,
            tuple(sorted(disclosures)),
            receipt_digest,
        )
        updated = replace(contract, stage=ContractStage.PRESENTED, presentation_receipt_digest=receipt_digest)
        self._save(updated, "PRESENTED", "ARKAON", "system", now, receipt_digest)
        return receipt

    def accept_partner_signature(self, envelope: SignatureEnvelope, now: datetime) -> ElectronicContract:
        contract = self._require(envelope.contract_id, ContractStage.PRESENTED, now)
        self._verify_envelope(envelope, contract, contract.partner, now)
        updated = replace(
            contract,
            stage=ContractStage.PARTNER_SIGNED,
            partner_signature_digest=envelope.provider_receipt_digest,
        )
        return self._save(updated, "PARTNER_SIGNED", "PARTNER_SIGNER", envelope.signer_id, now, envelope.provider_receipt_digest)

    def accept_partner_signature_and_auto_execute(
        self,
        envelope: SignatureEnvelope,
        *,
        mandate: ContractMandate,
        representative: RepresentativeVerification,
        now: datetime,
    ) -> ElectronicContract:
        contract = self._require(envelope.contract_id, ContractStage.PRESENTED, now)
        if contract.mandate_digest != mandate.digest or now >= mandate.expires_at:
            raise ContractRejected("active matching automatic execution mandate required")
        if (
            representative.partner_party_id != contract.partner.party_id
            or not representative.verified_at <= now < representative.expires_at
            or representative.verified_identity_subject_hash
            != sha256(contract.partner.authorized_signer_id.encode()).hexdigest()
        ):
            raise ContractRejected("verified registered business representative required")
        self._verify_envelope(envelope, contract, contract.partner, now)
        signed = replace(
            contract,
            stage=ContractStage.PARTNER_SIGNED,
            partner_signature_digest=envelope.provider_receipt_digest,
            representative_verification_digest=representative.provider_receipt_digest,
        )
        self._save(
            signed,
            "PARTNER_SIGNED",
            "VERIFIED_BUSINESS_REPRESENTATIVE",
            envelope.signer_id,
            now,
            envelope.provider_receipt_digest,
        )
        execution_digest = canonical_digest(
            {
                "document": signed.document_digest,
                "partner_signature": signed.partner_signature_digest,
                "mandate": mandate.digest,
                "payment": signed.payment_receipt_digest,
                "representative": signed.representative_verification_digest,
                "executed_at": now.isoformat(),
            }
        )
        executed = replace(
            signed,
            stage=ContractStage.AUTO_EXECUTED,
            execution_digest=execution_digest,
        )
        return self._save(
            executed,
            "AUTO_EXECUTED",
            "PREAPPROVED_POLICY_ENGINE",
            mandate.mandate_id,
            now,
            execution_digest,
        )

    def accept_company_signature(self, envelope: SignatureEnvelope, now: datetime) -> ElectronicContract:
        contract = self._require(envelope.contract_id, ContractStage.PARTNER_SIGNED, now)
        self._verify_envelope(envelope, contract, contract.company, now)
        updated = replace(
            contract,
            stage=ContractStage.COMPANY_SIGNED,
            company_signature_digest=envelope.provider_receipt_digest,
        )
        return self._save(updated, "COMPANY_SIGNED", "COMPANY_SIGNER", envelope.signer_id, now, envelope.provider_receipt_digest)

    def execute(self, contract_id: str, operator_ref: str, now: datetime) -> ElectronicContract:
        contract = self._require(contract_id, ContractStage.COMPANY_SIGNED, now)
        if not operator_ref.strip() or operator_ref == contract.company.authorized_signer_id:
            raise ContractRejected("post-signature operator separation required")
        execution_digest = canonical_digest(
            {
                "document": contract.document_digest,
                "partner_signature": contract.partner_signature_digest,
                "company_signature": contract.company_signature_digest,
                "executed_at": now.isoformat(),
            }
        )
        updated = replace(contract, stage=ContractStage.EXECUTED, execution_digest=execution_digest)
        return self._save(updated, "EXECUTED", "OPERATOR", operator_ref, now, execution_digest)

    def archive(self, contract_id: str, archive_digest: str, now: datetime) -> ElectronicContract:
        contract = self.get(contract_id)
        if contract.stage not in {ContractStage.EXECUTED, ContractStage.AUTO_EXECUTED}:
            raise ContractRejected("executed contract required")
        if not _digest_ok(archive_digest):
            raise ContractRejected("archive evidence digest required")
        updated = replace(contract, stage=ContractStage.ARCHIVED, archive_digest=archive_digest)
        return self._save(updated, "ARCHIVED", "RECORDS_MANAGER", "archive", now, archive_digest)

    def withdraw(self, contract_id: str, operator_ref: str, reason_digest: str, now: datetime) -> ElectronicContract:
        contract = self.get(contract_id)
        if contract.stage in {
            ContractStage.EXECUTED,
            ContractStage.AUTO_EXECUTED,
            ContractStage.ARCHIVED,
            ContractStage.WITHDRAWN,
        }:
            raise ContractRejected("immutable or already withdrawn contract")
        if not operator_ref.strip() or not _digest_ok(reason_digest):
            raise ContractRejected("operator and withdrawal evidence required")
        updated = replace(contract, stage=ContractStage.WITHDRAWN)
        return self._save(updated, "WITHDRAWN", "OPERATOR", operator_ref, now, reason_digest)

    def expire_due(self, contract_id: str, now: datetime) -> ElectronicContract:
        contract = self.get(contract_id)
        if now.tzinfo is None or now < contract.expires_at:
            raise ContractRejected("contract not due for expiry")
        if contract.stage in {
            ContractStage.EXECUTED,
            ContractStage.AUTO_EXECUTED,
            ContractStage.ARCHIVED,
            ContractStage.WITHDRAWN,
        }:
            raise ContractRejected("immutable terminal contract")
        updated = replace(contract, stage=ContractStage.EXPIRED)
        return self._save(updated, "EXPIRED", "ARKAON", "expiry-monitor", now, contract.document_digest)

    def evidence_bundle(self, contract_id: str) -> dict[str, Any]:
        contract = self.get(contract_id)
        events = [event for event in self.audit if event.contract_id == contract_id]
        return {
            "schema": "narang.electronic-partner-contract.v1",
            "contract_id": contract_id,
            "stage": contract.stage.value,
            "template_digest": contract.template_digest,
            "document_digest": contract.document_digest,
            "presentation_receipt_digest": contract.presentation_receipt_digest,
            "partner_signature_digest": contract.partner_signature_digest,
            "company_signature_digest": contract.company_signature_digest,
            "execution_digest": contract.execution_digest,
            "archive_digest": contract.archive_digest,
            "mandate_digest": contract.mandate_digest,
            "payment_receipt_digest": contract.payment_receipt_digest,
            "representative_verification_digest": contract.representative_verification_digest,
            "event_chain_head": events[-1].event_digest if events else None,
            "synthetic_only": True,
            "arkaon_is_contracting_party": False,
            "arkaon_signature_allowed": False,
            "production_execution_allowed": False,
            "automatic_execution_basis": "PREAPPROVED_OPERATOR_MANDATE",
        }

    def get(self, contract_id: str) -> ElectronicContract:
        try:
            return self._contracts[contract_id]
        except KeyError as exc:
            raise ContractRejected("contract not found") from exc

    def _require(
        self,
        contract_id: str,
        expected: ContractStage,
        now: datetime,
        *,
        allow_expired: bool = False,
    ) -> ElectronicContract:
        contract = self.get(contract_id)
        if contract.stage is not expected:
            raise ContractRejected(f"expected contract stage {expected.value}")
        if now.tzinfo is None or (not allow_expired and now >= contract.expires_at):
            raise ContractRejected("contract expired")
        return contract

    def _verify_envelope(
        self,
        envelope: SignatureEnvelope,
        contract: ElectronicContract,
        party: ContractParty,
        now: datetime,
    ) -> None:
        if envelope.envelope_id in self._used_envelope_ids or envelope.nonce in self._used_nonces:
            raise ContractRejected("signature replay")
        if (
            envelope.document_digest != contract.document_digest
            or envelope.party_id != party.party_id
            or envelope.signer_id != party.authorized_signer_id
            or envelope.signer_authority_digest != party.authority_evidence_digest
            or envelope.signed_at > now
            or envelope.signed_at >= contract.expires_at
        ):
            raise ContractRejected("signature envelope mismatch")
        self._used_envelope_ids.add(envelope.envelope_id)
        self._used_nonces.add(envelope.nonce)

    def _save(
        self,
        contract: ElectronicContract,
        action: str,
        actor_type: str,
        actor_ref: str,
        now: datetime,
        evidence_digest: str,
    ) -> ElectronicContract:
        self._contracts[contract.contract_id] = contract
        self._record(contract, action, actor_type, actor_ref, now, evidence_digest)
        return contract

    def _record(
        self,
        contract: ElectronicContract,
        action: str,
        actor_type: str,
        actor_ref: str,
        now: datetime,
        evidence_digest: str,
    ) -> None:
        if actor_type == "ARKAON" and action not in {"ARKAON_PREPARED", "PRESENTED", "EXPIRED"}:
            raise ContractRejected("ARKAON cannot approve or sign")
        previous = self.audit[-1].event_digest if self.audit else "0" * 64
        sequence = len(self.audit) + 1
        event_digest = canonical_digest(
            (sequence, contract.contract_id, action, actor_type, actor_ref, now.isoformat(), evidence_digest, previous)
        )
        self.audit.append(
            ContractAuditEvent(sequence, contract.contract_id, action, actor_type, actor_ref, now, evidence_digest, previous, event_digest)
        )

    @staticmethod
    def _render(text: str, variables: dict[str, str]) -> str:
        rendered = text
        for key, value in variables.items():
            rendered = rendered.replace("{{" + key + "}}", value)
        if "{{" in rendered or "}}" in rendered:
            raise ContractRejected("unresolved or unapproved template variable")
        return rendered
