"""Integrated, non-activating release gate for map capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256


class GateRejected(ValueError):
    pass


class GateVerdict(StrEnum):
    BLOCKED = "blocked"
    READY_FOR_OPERATOR_DECISION = "ready_for_operator_decision"


REQUIRED_INTERNAL_EVIDENCE = frozenset(
    {
        "document_monitor",
        "sandbox_contract",
        "device_certification",
        "route_quality_shadow",
        "provider_resilience",
    }
)

REQUIRED_EXTERNAL_GATES = frozenset({"EXT-02", "EXT-03", "EXT-04", "EXT-05", "EXT-07"})


@dataclass(frozen=True)
class InternalEvidence:
    evidence_type: str
    status: str
    digest: str
    source_commit: str
    generated_at: datetime
    expires_at: datetime
    synthetic_only: bool

    def valid_for(self, candidate_commit: str, now: datetime) -> bool:
        return (
            self.evidence_type in REQUIRED_INTERNAL_EVIDENCE
            and self.status == "pass_for_human_review"
            and len(self.digest) == 64
            and all(character in "0123456789abcdef" for character in self.digest)
            and len(self.source_commit) == 40
            and self.source_commit == candidate_commit
            and self.generated_at <= now < self.expires_at
            and self.synthetic_only
        )


@dataclass(frozen=True)
class ExternalGate:
    gate_id: str
    status: str
    evidence_digest: str | None = None
    evidence_expires_at: datetime | None = None
    ethernian_review_ref: str | None = None
    operator_approval_ref: str | None = None

    def verified(self, now: datetime) -> bool:
        return (
            self.gate_id in REQUIRED_EXTERNAL_GATES
            and self.status == "VERIFIED"
            and self.evidence_digest is not None
            and len(self.evidence_digest) == 64
            and self.evidence_expires_at is not None
            and now < self.evidence_expires_at
            and bool(self.ethernian_review_ref)
        )


@dataclass(frozen=True)
class MapReleaseGateReport:
    candidate_commit: str
    verdict: GateVerdict
    internal_evidence_types: tuple[str, ...]
    verified_external_gates: tuple[str, ...]
    blockers: tuple[str, ...]
    report_digest: str
    operator_decision_required: bool = True
    main_merge_allowed: bool = False
    deployment_allowed: bool = False
    production_activation_allowed: bool = False

    def as_ci_artifact(self) -> dict[str, object]:
        return {"schema": "narang.map-release-gate.v1", **self.__dict__}


class MapReleaseGate:
    def evaluate(
        self,
        *,
        candidate_commit: str,
        internal_evidence: tuple[InternalEvidence, ...],
        external_gates: tuple[ExternalGate, ...],
        ethernian_final_review_ref: str | None,
        operator_decision_ref: str | None,
        now: datetime,
    ) -> MapReleaseGateReport:
        if (
            len(candidate_commit) != 40
            or any(character not in "0123456789abcdef" for character in candidate_commit)
        ):
            raise GateRejected("full lowercase candidate commit SHA required")
        internal_by_type = self._unique_internal(internal_evidence)
        external_by_id = self._unique_external(external_gates)
        blockers: list[str] = []

        for evidence_type in sorted(REQUIRED_INTERNAL_EVIDENCE):
            item = internal_by_type.get(evidence_type)
            if item is None:
                blockers.append(f"internal_missing:{evidence_type}")
            elif not item.valid_for(candidate_commit, now):
                blockers.append(f"internal_invalid:{evidence_type}")

        verified_external: list[str] = []
        for gate_id in sorted(REQUIRED_EXTERNAL_GATES):
            gate = external_by_id.get(gate_id)
            if gate is None:
                blockers.append(f"external_missing:{gate_id}")
            elif not gate.verified(now):
                blockers.append(f"external_not_verified:{gate_id}")
            else:
                verified_external.append(gate_id)

        if not ethernian_final_review_ref:
            blockers.append("ethernian_final_review_missing")
        if not operator_decision_ref:
            blockers.append("operator_decision_missing")
        if (
            ethernian_final_review_ref
            and operator_decision_ref
            and ethernian_final_review_ref == operator_decision_ref
        ):
            blockers.append("independent_approval_separation_failed")

        verdict = (
            GateVerdict.READY_FOR_OPERATOR_DECISION
            if not blockers
            else GateVerdict.BLOCKED
        )
        internal_types = tuple(sorted(internal_by_type))
        parts = (
            candidate_commit,
            verdict.value,
            ",".join(internal_types),
            ",".join(verified_external),
            ",".join(blockers),
        )
        return MapReleaseGateReport(
            candidate_commit=candidate_commit,
            verdict=verdict,
            internal_evidence_types=internal_types,
            verified_external_gates=tuple(verified_external),
            blockers=tuple(blockers),
            report_digest=sha256("|".join(parts).encode()).hexdigest(),
        )

    @staticmethod
    def _unique_internal(
        items: tuple[InternalEvidence, ...],
    ) -> dict[str, InternalEvidence]:
        result: dict[str, InternalEvidence] = {}
        for item in items:
            if item.evidence_type in result:
                raise GateRejected("duplicate internal evidence type")
            result[item.evidence_type] = item
        return result

    @staticmethod
    def _unique_external(items: tuple[ExternalGate, ...]) -> dict[str, ExternalGate]:
        result: dict[str, ExternalGate] = {}
        for item in items:
            if item.gate_id in result:
                raise GateRejected("duplicate external gate")
            result[item.gate_id] = item
        return result
