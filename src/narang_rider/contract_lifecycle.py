"""Synthetic post-execution partner contract lifecycle governance."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256

from narang_rider.electronic_contract import ContractStage, ElectronicContract


class LifecycleRejected(ValueError):
    pass


class LifecycleStage(StrEnum):
    PENDING_ACTIVATION = "pending_activation"
    ACTIVE = "active"
    HUMAN_REVIEW = "human_review"
    SUSPENDED = "suspended"
    TERMINATION_PENDING = "termination_pending"
    TERMINATED = "terminated"
    EXPIRED = "expired"


class Recommendation(StrEnum):
    NONE = "none"
    REVIEW_RENEWAL = "review_renewal"
    REVIEW_COMPLIANCE = "review_compliance"
    REVIEW_PAYMENT = "review_payment"


def _digest_ok(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class EntitlementMandate:
    mandate_id: str
    contract_id: str
    contract_execution_digest: str
    partner_party_id: str
    vertical: str
    entitlements: frozenset[str]
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    synthetic: bool = True

    def __post_init__(self) -> None:
        if (
            not self.mandate_id
            or not self.contract_id
            or not self.partner_party_id
            or not self.vertical
            or not self.entitlements
            or not self.approved_by
            or not _digest_ok(self.contract_execution_digest)
            or self.approved_at.tzinfo is None
            or not self.approved_at < self.expires_at
        ):
            raise LifecycleRejected("complete bounded entitlement mandate required")
        if any(not item or item != item.upper() for item in self.entitlements):
            raise LifecycleRejected("canonical explicit entitlements required")
        if not self.synthetic:
            raise LifecycleRejected("production entitlement mandate is forbidden")

    @property
    def digest(self) -> str:
        return _digest(
            {
                "mandate_id": self.mandate_id,
                "contract_id": self.contract_id,
                "contract_execution_digest": self.contract_execution_digest,
                "partner_party_id": self.partner_party_id,
                "vertical": self.vertical,
                "entitlements": sorted(self.entitlements),
                "approved_by": self.approved_by,
                "approved_at": self.approved_at.isoformat(),
                "expires_at": self.expires_at.isoformat(),
                "synthetic": self.synthetic,
            }
        )


@dataclass(frozen=True)
class PartnerLifecycle:
    lifecycle_id: str
    contract_id: str
    contract_document_digest: str
    contract_execution_digest: str
    partner_party_id: str
    vertical: str
    entitlements: frozenset[str]
    mandate_digest: str
    starts_at: datetime
    ends_at: datetime
    stage: LifecycleStage = LifecycleStage.PENDING_ACTIVATION
    recommendation: Recommendation = Recommendation.NONE
    decision_digest: str | None = None
    superseded_by_contract_id: str | None = None
    synthetic: bool = True

    def __post_init__(self) -> None:
        if (
            not self.lifecycle_id
            or not _digest_ok(self.contract_document_digest)
            or not _digest_ok(self.contract_execution_digest)
            or not _digest_ok(self.mandate_digest)
            or self.starts_at.tzinfo is None
            or not self.starts_at < self.ends_at
            or not self.synthetic
        ):
            raise LifecycleRejected("valid synthetic lifecycle required")


@dataclass(frozen=True)
class LifecycleSignal:
    signal_id: str
    partner_party_id: str
    kind: str
    observed_at: datetime
    evidence_digest: str
    synthetic: bool = True

    def __post_init__(self) -> None:
        if (
            not self.signal_id
            or self.kind not in {"RENEWAL_WINDOW", "COMPLIANCE_STALE", "PAYMENT_OVERDUE"}
            or self.observed_at.tzinfo is None
            or not _digest_ok(self.evidence_digest)
            or not self.synthetic
        ):
            raise LifecycleRejected("bounded synthetic monitoring signal required")


@dataclass(frozen=True)
class LifecycleAuditEvent:
    sequence: int
    lifecycle_id: str
    action: str
    actor_type: str
    actor_ref: str
    occurred_at: datetime
    evidence_digest: str
    previous_event_digest: str
    event_digest: str


class ContractLifecycleService:
    """Activates exact grants and keeps ARKAON advisory-only after execution."""

    def __init__(self) -> None:
        self._items: dict[str, PartnerLifecycle] = {}
        self._contract_ids: set[str] = set()
        self._signal_ids: set[str] = set()
        self.audit: list[LifecycleAuditEvent] = []

    def activate(
        self,
        *,
        lifecycle_id: str,
        contract: ElectronicContract,
        mandate: EntitlementMandate,
        starts_at: datetime,
        ends_at: datetime,
    ) -> PartnerLifecycle:
        if lifecycle_id in self._items or contract.contract_id in self._contract_ids:
            raise LifecycleRejected("duplicate activation forbidden")
        if contract.stage not in {ContractStage.EXECUTED, ContractStage.AUTO_EXECUTED}:
            raise LifecycleRejected("executed contract required")
        if starts_at.tzinfo is None or ends_at.tzinfo is None:
            raise LifecycleRejected("timezone-aware activation window required")
        if (
            mandate.contract_id != contract.contract_id
            or mandate.contract_execution_digest != contract.execution_digest
            or mandate.partner_party_id != contract.partner.party_id
            or starts_at < mandate.approved_at
            or starts_at >= mandate.expires_at
            or ends_at > contract.expires_at
        ):
            raise LifecycleRejected("contract and entitlement mandate mismatch")
        lifecycle = PartnerLifecycle(
            lifecycle_id=lifecycle_id,
            contract_id=contract.contract_id,
            contract_document_digest=contract.document_digest,
            contract_execution_digest=contract.execution_digest or "",
            partner_party_id=contract.partner.party_id,
            vertical=mandate.vertical,
            entitlements=mandate.entitlements,
            mandate_digest=mandate.digest,
            starts_at=starts_at,
            ends_at=ends_at,
            stage=LifecycleStage.ACTIVE,
        )
        self._items[lifecycle_id] = lifecycle
        self._contract_ids.add(contract.contract_id)
        self._record(
            lifecycle,
            "ACTIVATED",
            "PREAPPROVED_POLICY_ENGINE",
            mandate.approved_by,
            starts_at,
            mandate.digest,
        )
        return lifecycle

    def observe(self, lifecycle_id: str, signal: LifecycleSignal) -> PartnerLifecycle:
        lifecycle = self.get(lifecycle_id)
        if signal.signal_id in self._signal_ids:
            raise LifecycleRejected("monitoring signal replay")
        if lifecycle.stage is not LifecycleStage.ACTIVE:
            raise LifecycleRejected("only active lifecycle may be monitored")
        if not lifecycle.starts_at <= signal.observed_at < lifecycle.ends_at:
            raise LifecycleRejected("monitoring signal outside lifecycle window")
        if signal.partner_party_id != lifecycle.partner_party_id:
            raise LifecycleRejected("cross-partner signal forbidden")
        recommendation = {
            "RENEWAL_WINDOW": Recommendation.REVIEW_RENEWAL,
            "COMPLIANCE_STALE": Recommendation.REVIEW_COMPLIANCE,
            "PAYMENT_OVERDUE": Recommendation.REVIEW_PAYMENT,
        }[signal.kind]
        self._signal_ids.add(signal.signal_id)
        updated = replace(
            lifecycle,
            stage=LifecycleStage.HUMAN_REVIEW,
            recommendation=recommendation,
        )
        return self._save(
            updated,
            "REVIEW_RECOMMENDED",
            "ARKAON",
            "monitor",
            signal.observed_at,
            signal.evidence_digest,
        )

    def continue_after_review(
        self, lifecycle_id: str, *, decision_ref: str, decision_digest: str, now: datetime
    ) -> PartnerLifecycle:
        lifecycle = self._require(lifecycle_id, LifecycleStage.HUMAN_REVIEW, now)
        if not decision_ref or not _digest_ok(decision_digest):
            raise LifecycleRejected("operator review decision required")
        updated = replace(
            lifecycle,
            stage=LifecycleStage.ACTIVE,
            recommendation=Recommendation.NONE,
            decision_digest=decision_digest,
        )
        return self._save(updated, "CONTINUED", "OPERATOR", decision_ref, now, decision_digest)

    def suspend(
        self, lifecycle_id: str, *, decision_ref: str, decision_digest: str, now: datetime
    ) -> PartnerLifecycle:
        lifecycle = self.get(lifecycle_id)
        self._validate_decision_time(lifecycle, now)
        if lifecycle.stage not in {LifecycleStage.ACTIVE, LifecycleStage.HUMAN_REVIEW}:
            raise LifecycleRejected("active or reviewed lifecycle required")
        if not decision_ref or not _digest_ok(decision_digest):
            raise LifecycleRejected("operator suspension decision required")
        updated = replace(
            lifecycle,
            stage=LifecycleStage.SUSPENDED,
            recommendation=Recommendation.NONE,
            decision_digest=decision_digest,
        )
        return self._save(updated, "SUSPENDED", "OPERATOR", decision_ref, now, decision_digest)

    def request_termination(
        self, lifecycle_id: str, *, requester_ref: str, decision_digest: str, now: datetime
    ) -> PartnerLifecycle:
        lifecycle = self.get(lifecycle_id)
        self._validate_decision_time(lifecycle, now)
        if lifecycle.stage not in {
            LifecycleStage.ACTIVE,
            LifecycleStage.HUMAN_REVIEW,
            LifecycleStage.SUSPENDED,
        }:
            raise LifecycleRejected("terminable lifecycle required")
        if not requester_ref or not _digest_ok(decision_digest):
            raise LifecycleRejected("termination request evidence required")
        updated = replace(
            lifecycle,
            stage=LifecycleStage.TERMINATION_PENDING,
            decision_digest=decision_digest,
        )
        return self._save(
            updated,
            "TERMINATION_REQUESTED",
            "OPERATOR",
            requester_ref,
            now,
            decision_digest,
        )

    def confirm_termination(
        self, lifecycle_id: str, *, confirmer_ref: str, confirmation_digest: str, now: datetime
    ) -> PartnerLifecycle:
        lifecycle = self._require(lifecycle_id, LifecycleStage.TERMINATION_PENDING, now)
        request_event = next(
            event for event in reversed(self.audit)
            if event.lifecycle_id == lifecycle_id and event.action == "TERMINATION_REQUESTED"
        )
        if (
            not confirmer_ref
            or confirmer_ref == request_event.actor_ref
            or not _digest_ok(confirmation_digest)
        ):
            raise LifecycleRejected("independent termination confirmation required")
        updated = replace(
            lifecycle,
            stage=LifecycleStage.TERMINATED,
            decision_digest=confirmation_digest,
        )
        return self._save(
            updated, "TERMINATED", "OPERATOR", confirmer_ref, now, confirmation_digest
        )

    def expire_due(self, lifecycle_id: str, now: datetime) -> PartnerLifecycle:
        lifecycle = self.get(lifecycle_id)
        if now.tzinfo is None:
            raise LifecycleRejected("timezone-aware expiry time required")
        if (
            lifecycle.stage in {LifecycleStage.TERMINATED, LifecycleStage.EXPIRED}
            or now < lifecycle.ends_at
        ):
            raise LifecycleRejected("lifecycle is not eligible for expiry")
        updated = replace(
            lifecycle,
            stage=LifecycleStage.EXPIRED,
            recommendation=Recommendation.NONE,
        )
        return self._save(
            updated,
            "EXPIRED",
            "POLICY_CLOCK",
            "system",
            now,
            lifecycle.contract_execution_digest,
        )

    def link_superseding_contract(
        self,
        lifecycle_id: str,
        *,
        new_contract: ElectronicContract,
        operator_ref: str,
        now: datetime,
    ) -> PartnerLifecycle:
        lifecycle = self.get(lifecycle_id)
        self._validate_decision_time(lifecycle, now)
        if (
            new_contract.stage not in {ContractStage.EXECUTED, ContractStage.AUTO_EXECUTED}
            or not new_contract.execution_digest
            or new_contract.supersedes_contract_id != lifecycle.contract_id
            or new_contract.partner.party_id != lifecycle.partner_party_id
            or not operator_ref
        ):
            raise LifecycleRejected("executed same-partner superseding contract required")
        updated = replace(lifecycle, superseded_by_contract_id=new_contract.contract_id)
        return self._save(
            updated,
            "SUPERSEDING_CONTRACT_LINKED",
            "OPERATOR",
            operator_ref,
            now,
            new_contract.execution_digest or "",
        )

    def arkaon_action(self, action: str) -> None:
        if action not in {"OBSERVE", "EXPLAIN", "RECOMMEND_REVIEW"}:
            raise LifecycleRejected("ARKAON lifecycle authority forbidden")

    def get(self, lifecycle_id: str) -> PartnerLifecycle:
        try:
            return self._items[lifecycle_id]
        except KeyError as exc:
            raise LifecycleRejected("unknown lifecycle") from exc

    def evidence_bundle(self, lifecycle_id: str) -> dict[str, object]:
        lifecycle = self.get(lifecycle_id)
        events = [event for event in self.audit if event.lifecycle_id == lifecycle_id]
        return {
            "schema_version": "narang.contract-lifecycle.v1",
            "lifecycle": lifecycle,
            "event_chain_head": events[-1].event_digest,
            "arkaon_can_change_entitlements": False,
            "arkaon_can_suspend_or_terminate": False,
            "production_activation_allowed": False,
        }

    def _require(self, lifecycle_id: str, stage: LifecycleStage, now: datetime) -> PartnerLifecycle:
        lifecycle = self.get(lifecycle_id)
        if lifecycle.stage is not stage:
            raise LifecycleRejected(f"expected lifecycle stage {stage.value}")
        if now.tzinfo is None or now >= lifecycle.ends_at:
            raise LifecycleRejected("lifecycle expired")
        return lifecycle

    @staticmethod
    def _validate_decision_time(lifecycle: PartnerLifecycle, now: datetime) -> None:
        if now.tzinfo is None or now >= lifecycle.ends_at:
            raise LifecycleRejected("lifecycle expired")

    def _save(
        self,
        lifecycle: PartnerLifecycle,
        action: str,
        actor_type: str,
        actor_ref: str,
        now: datetime,
        evidence_digest: str,
    ) -> PartnerLifecycle:
        if actor_type == "ARKAON" and action != "REVIEW_RECOMMENDED":
            raise LifecycleRejected("ARKAON cannot decide lifecycle state")
        self._items[lifecycle.lifecycle_id] = lifecycle
        self._record(lifecycle, action, actor_type, actor_ref, now, evidence_digest)
        return lifecycle

    def _record(
        self,
        lifecycle: PartnerLifecycle,
        action: str,
        actor_type: str,
        actor_ref: str,
        now: datetime,
        evidence_digest: str,
    ) -> None:
        previous = self.audit[-1].event_digest if self.audit else "0" * 64
        sequence = len(self.audit) + 1
        event_digest = _digest(
            (
                sequence,
                lifecycle.lifecycle_id,
                action,
                actor_type,
                actor_ref,
                now.isoformat(),
                evidence_digest,
                previous,
            )
        )
        self.audit.append(
            LifecycleAuditEvent(
                sequence,
                lifecycle.lifecycle_id,
                action,
                actor_type,
                actor_ref,
                now,
                evidence_digest,
                previous,
                event_digest,
            )
        )
