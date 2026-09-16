"""Operator-to-ARKAON conversation and review-branch change-control contracts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath

from narang_rider.contributor_guidance import ArkaonContributorGuide


class AdminArkaonRejected(ValueError):
    pass


class ProposalStage(StrEnum):
    PROPOSED = "PROPOSED"
    PREVIEWED = "PREVIEWED"
    OPERATOR_APPROVED = "OPERATOR_APPROVED"
    REVIEW_BRANCH_APPLIED = "REVIEW_BRANCH_APPLIED"
    REJECTED = "REJECTED"


def digest(value: object) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ConversationMessage:
    message_id: str
    session_id: str
    actor_type: str
    actor_ref: str
    text: str
    created_at: datetime
    previous_digest: str
    message_digest: str


@dataclass(frozen=True)
class PartialChangeInstruction:
    instruction_id: str
    session_id: str
    requested_by: str
    scope: str
    requested_paths: tuple[str, ...]
    requirement: str
    expected_head: str
    created_at: datetime


@dataclass(frozen=True)
class ChangeProposal:
    proposal_id: str
    instruction_id: str
    source_head: str
    changed_paths: tuple[str, ...]
    summary: str
    patch_digest: str
    test_plan: tuple[str, ...]
    risk_notes: tuple[str, ...]
    stage: ProposalStage
    version: int
    approved_by: str | None = None
    approval_digest: str | None = None
    applied_evidence_digest: str | None = None
    guidance_version: int = 0
    guidance_digest: str | None = None


@dataclass(frozen=True)
class GuidanceSnapshot:
    proposal_id: str
    proposal_version: int
    guidance_version: int
    changed_paths: tuple[str, ...]
    required_open_count: int
    guidance_digest: str
    generated_at: datetime


ADMIN_ARKAON_ROUTES = (
    ("POST", "/api/v1/admin/arkaon/sessions", "ArkaonSessionCreateV1"),
    ("POST", "/api/v1/admin/arkaon/sessions/{session_id}/messages", "ArkaonMessageV1"),
    ("POST", "/api/v1/admin/arkaon/change-instructions", "PartialChangeInstructionV1"),
    ("POST", "/api/v1/admin/arkaon/proposals/{proposal_id}/preview", "ChangePreviewV1"),
    ("POST", "/api/v1/admin/arkaon/proposals/{proposal_id}/approve", "ChangeApprovalV1"),
    ("POST", "/api/v1/admin/arkaon/proposals/{proposal_id}/apply-review", "ReviewApplyV1"),
    ("GET", "/api/v1/admin/arkaon/proposals/{proposal_id}/guidance", "GuidanceSnapshotV1"),
)


class AdminArkaonControl:
    """Allows bounded review-branch proposals; never production mutation or self-approval."""

    SAFE_PREFIXES = ("src/", "tests/", "docs/", "config/", "frontend/", ".github/")
    FORBIDDEN_PATHS = (".env", "secrets/", "credentials/", ".git/")
    FORBIDDEN_TEXT = re.compile(
        r"(?:BEGIN (?:RSA|OPENSSH) PRIVATE KEY|AKIA[0-9A-Z]{16}|\b\d{6}-[1-4]\d{6}\b)"
    )

    def __init__(self) -> None:
        self._messages: dict[str, list[ConversationMessage]] = {}
        self._instructions: dict[str, PartialChangeInstruction] = {}
        self._proposals: dict[str, ChangeProposal] = {}
        self._guidance: dict[str, list[GuidanceSnapshot]] = {}
        self._used_approval_digests: set[str] = set()

    def start_session(self, session_id: str, *, operator_id: str, now: datetime) -> None:
        if not session_id or not operator_id or now.tzinfo is None or session_id in self._messages:
            raise AdminArkaonRejected("valid unique operator session required")
        self._messages[session_id] = []

    def post_message(
        self,
        session_id: str,
        *,
        message_id: str,
        actor_type: str,
        actor_ref: str,
        text: str,
        now: datetime,
    ) -> ConversationMessage:
        if session_id not in self._messages:
            raise AdminArkaonRejected("unknown session")
        if actor_type not in {"OPERATOR", "ARKAON"} or not actor_ref or now.tzinfo is None:
            raise AdminArkaonRejected("authorized conversation actor required")
        if not text.strip() or len(text) > 4_000 or self.FORBIDDEN_TEXT.search(text):
            raise AdminArkaonRejected("safe bounded message required")
        values = self._messages[session_id]
        if any(item.message_id == message_id for item in values):
            raise AdminArkaonRejected("message replay")
        previous = values[-1].message_digest if values else "0" * 64
        message_digest = digest(
            (message_id, session_id, actor_type, actor_ref, text, now.isoformat(), previous)
        )
        message = ConversationMessage(
            message_id, session_id, actor_type, actor_ref, text, now, previous, message_digest
        )
        values.append(message)
        return message

    def request_partial_change(self, instruction: PartialChangeInstruction) -> PartialChangeInstruction:
        if instruction.session_id not in self._messages:
            raise AdminArkaonRejected("unknown session")
        if instruction.instruction_id in self._instructions:
            raise AdminArkaonRejected("instruction replay")
        if (
            not instruction.requested_by
            or not instruction.scope.strip()
            or not instruction.requirement.strip()
            or instruction.created_at.tzinfo is None
            or not self._commit_sha_ok(instruction.expected_head)
            or not instruction.requested_paths
        ):
            raise AdminArkaonRejected("complete bounded instruction required")
        safe_paths = tuple(sorted({self._safe_path(path) for path in instruction.requested_paths}))
        value = replace(instruction, requested_paths=safe_paths)
        self._instructions[value.instruction_id] = value
        return value

    def propose(
        self,
        *,
        proposal_id: str,
        instruction_id: str,
        source_head: str,
        changed_paths: tuple[str, ...],
        summary: str,
        patch_digest: str,
        test_plan: tuple[str, ...],
        risk_notes: tuple[str, ...],
        now: datetime,
    ) -> ChangeProposal:
        instruction = self._instructions[instruction_id]
        paths = tuple(sorted({self._safe_path(path) for path in changed_paths}))
        if (
            proposal_id in self._proposals
            or source_head != instruction.expected_head
            or not set(paths) <= set(instruction.requested_paths)
            or not summary.strip()
            or not self._digest_ok(patch_digest)
            or not test_plan
            or not risk_notes
        ):
            raise AdminArkaonRejected("bounded proposal matching instruction required")
        proposal = ChangeProposal(
            proposal_id,
            instruction_id,
            source_head,
            paths,
            summary,
            patch_digest,
            test_plan,
            risk_notes,
            ProposalStage.PROPOSED,
            1,
        )
        self._proposals[proposal_id] = proposal
        self._refresh_guidance(proposal, now)
        return self._proposals[proposal_id]

    def preview(self, proposal_id: str, *, expected_version: int, now: datetime) -> ChangeProposal:
        proposal = self._require(proposal_id, ProposalStage.PROPOSED, expected_version)
        updated = replace(proposal, stage=ProposalStage.PREVIEWED, version=proposal.version + 1)
        self._proposals[proposal_id] = updated
        self._refresh_guidance(updated, now)
        return self._proposals[proposal_id]

    def approve(
        self,
        proposal_id: str,
        *,
        operator_id: str,
        approval_digest: str,
        expected_version: int,
        now: datetime,
    ) -> ChangeProposal:
        proposal = self._require(proposal_id, ProposalStage.PREVIEWED, expected_version)
        instruction = self._instructions[proposal.instruction_id]
        if (
            not operator_id
            or operator_id != instruction.requested_by
            or operator_id == "ARKAON"
            or not self._digest_ok(approval_digest)
            or approval_digest in self._used_approval_digests
        ):
            raise AdminArkaonRejected("fresh requesting-operator approval required")
        self._used_approval_digests.add(approval_digest)
        updated = replace(
            proposal,
            stage=ProposalStage.OPERATOR_APPROVED,
            version=proposal.version + 1,
            approved_by=operator_id,
            approval_digest=approval_digest,
        )
        self._proposals[proposal_id] = updated
        self._refresh_guidance(updated, now)
        return self._proposals[proposal_id]

    def apply_to_review_branch(
        self,
        proposal_id: str,
        *,
        applied_evidence_digest: str,
        expected_version: int,
        now: datetime,
    ) -> ChangeProposal:
        proposal = self._require(proposal_id, ProposalStage.OPERATOR_APPROVED, expected_version)
        if not self._digest_ok(applied_evidence_digest):
            raise AdminArkaonRejected("review-branch application evidence required")
        updated = replace(
            proposal,
            stage=ProposalStage.REVIEW_BRANCH_APPLIED,
            version=proposal.version + 1,
            applied_evidence_digest=applied_evidence_digest,
        )
        self._proposals[proposal_id] = updated
        self._refresh_guidance(updated, now)
        return self._proposals[proposal_id]

    def guidance(self, proposal_id: str) -> tuple[GuidanceSnapshot, ...]:
        return tuple(self._guidance.get(proposal_id, ()))

    def production_mutation(self) -> None:
        raise AdminArkaonRejected("production mutation, merge and deployment are forbidden")

    def _refresh_guidance(self, proposal: ChangeProposal, now: datetime) -> None:
        report = ArkaonContributorGuide().analyze(proposal.changed_paths)
        history = self._guidance.setdefault(proposal.proposal_id, [])
        version = len(history) + 1
        snapshot = GuidanceSnapshot(
            proposal.proposal_id,
            proposal.version,
            version,
            proposal.changed_paths,
            report.required_open_count,
            report.report_digest,
            now,
        )
        history.append(snapshot)
        self._proposals[proposal.proposal_id] = replace(
            self._proposals[proposal.proposal_id],
            guidance_version=version,
            guidance_digest=report.report_digest,
        )

    def _require(
        self, proposal_id: str, stage: ProposalStage, expected_version: int
    ) -> ChangeProposal:
        proposal = self._proposals[proposal_id]
        if proposal.stage is not stage or proposal.version != expected_version:
            raise AdminArkaonRejected("stale proposal or invalid stage")
        return proposal

    def _safe_path(self, path: str) -> str:
        stripped = path.strip()
        candidate = PurePosixPath(stripped)
        normalized = candidate.as_posix()
        if (
            not stripped
            or candidate.is_absolute()
            or ".." in candidate.parts
            or any(normalized.startswith(value) for value in self.FORBIDDEN_PATHS)
            or not any(normalized.startswith(value) for value in self.SAFE_PREFIXES)
        ):
            raise AdminArkaonRejected("safe allowlisted review path required")
        return normalized

    @staticmethod
    def _digest_ok(value: str) -> bool:
        return len(value) == 64 and all(character in "0123456789abcdef" for character in value)

    @staticmethod
    def _commit_sha_ok(value: str) -> bool:
        return len(value) == 40 and all(character in "0123456789abcdef" for character in value)
