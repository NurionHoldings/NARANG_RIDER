"""Privacy-preserving ARKAON guidance for repository contributors and coding agents."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath


class GuidanceRejected(ValueError):
    pass


class GuidancePriority(StrEnum):
    REQUIRED = "REQUIRED"
    RECOMMENDED = "RECOMMENDED"
    INFORMATIONAL = "INFORMATIONAL"


@dataclass(frozen=True)
class GuidanceRule:
    rule_id: str
    path_prefixes: tuple[str, ...]
    required_companion_prefixes: tuple[str, ...]
    title: str
    instruction: str
    owner_role: str
    priority: GuidancePriority = GuidancePriority.REQUIRED


@dataclass(frozen=True)
class GuidanceItem:
    rule_id: str
    priority: GuidancePriority
    title: str
    instruction: str
    owner_role: str
    matched_paths: tuple[str, ...]
    satisfied: bool


@dataclass(frozen=True)
class ContributorGuidanceReport:
    schema_version: str
    changed_paths: tuple[str, ...]
    items: tuple[GuidanceItem, ...]
    required_open_count: int
    advisory_only: bool
    visitor_tracking_used: bool
    automatic_approval_allowed: bool
    report_digest: str


DEFAULT_RULES = (
    GuidanceRule(
        "SOURCE_REQUIRES_TEST",
        ("src/",),
        ("tests/",),
        "소스 변경에 대응하는 시험을 추가하십시오.",
        "정상·실패·권한경계·재전송 또는 동시성 경로를 합성시험으로 고정합니다.",
        "ENGINEERING_LEAD",
    ),
    GuidanceRule(
        "POLICY_REQUIRES_EVIDENCE",
        ("config/",),
        ("scripts/run_", "tests/"),
        "정책 변경에 기계판독 증거를 연결하십시오.",
        "정책 digest, 생성 스크립트와 실패 폐쇄 시험이 같은 후보 커밋을 참조해야 합니다.",
        "ASSURANCE_LEAD",
    ),
    GuidanceRule(
        "WORKFLOW_REQUIRES_TEST_OR_DOC",
        (".github/workflows/",),
        ("tests/", "docs/"),
        "Workflow 변경의 목적과 검증 경로를 남기십시오.",
        "권한 최소화, fork 안전성, artifact 보존기간과 실패 동작을 검토합니다.",
        "RELEASE_ENGINEER",
    ),
    GuidanceRule(
        "MIGRATION_REQUIRES_RECOVERY",
        ("migrations/", "alembic/"),
        ("tests/", "docs/"),
        "Migration 복구 및 호환성 증거가 필요합니다.",
        "전진·후퇴·부분실패·기존 데이터 보존 시험을 추가합니다.",
        "DATABASE_OWNER",
    ),
    GuidanceRule(
        "FRONTEND_REQUIRES_ACCESSIBILITY",
        ("frontend/",),
        ("frontend/",),
        "Frontend 접근성·권한경계·계약 호환성을 확인하십시오.",
        "typecheck, 단위시험, 접근성 감사와 OpenAPI 호환성을 실행합니다.",
        "FRONTEND_LEAD",
        GuidancePriority.RECOMMENDED,
    ),
    GuidanceRule(
        "CONTRACT_REQUIRES_HUMAN_REVIEW",
        ("src/narang_rider/electronic_contract", "config/mobility-plaza-contract"),
        ("tests/", "docs/"),
        "전자계약 변경은 독립 법률·개인정보 검토 대상으로 표시하십시오.",
        "ARKAON의 계약당사자·서명자·자체승인 권한을 만들지 마십시오.",
        "LEGAL_REVIEWER",
    ),
)


class ArkaonContributorGuide:
    """Generates pre-emptive guidance without identifying or profiling visitors."""

    def __init__(self, rules: tuple[GuidanceRule, ...] = DEFAULT_RULES) -> None:
        identifiers = [rule.rule_id for rule in rules]
        if len(set(identifiers)) != len(identifiers):
            raise GuidanceRejected("duplicate guidance rule")
        self.rules = rules

    def analyze(self, changed_paths: tuple[str, ...]) -> ContributorGuidanceReport:
        paths = tuple(sorted({self._validate_path(path) for path in changed_paths}))
        items: list[GuidanceItem] = []
        for rule in self.rules:
            matched = tuple(
                path for path in paths if any(path.startswith(prefix) for prefix in rule.path_prefixes)
            )
            if not matched:
                continue
            satisfied = any(
                path.startswith(prefix)
                for path in paths
                for prefix in rule.required_companion_prefixes
            )
            items.append(
                GuidanceItem(
                    rule.rule_id,
                    rule.priority,
                    rule.title,
                    rule.instruction,
                    rule.owner_role,
                    matched,
                    satisfied,
                )
            )
        required_open = sum(
            not item.satisfied and item.priority is GuidancePriority.REQUIRED for item in items
        )
        payload = {
            "schema_version": "narang.arkaon-contributor-guidance.v1",
            "changed_paths": paths,
            "items": [
                {
                    **item.__dict__,
                    "priority": item.priority.value,
                }
                for item in items
            ],
            "required_open_count": required_open,
            "advisory_only": True,
            "visitor_tracking_used": False,
            "automatic_approval_allowed": False,
        }
        return ContributorGuidanceReport(
            schema_version=payload["schema_version"],
            changed_paths=paths,
            items=tuple(items),
            required_open_count=required_open,
            advisory_only=True,
            visitor_tracking_used=False,
            automatic_approval_allowed=False,
            report_digest=sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )

    def approve_or_merge(self) -> None:
        raise GuidanceRejected("ARKAON contributor guidance cannot approve or merge")

    @staticmethod
    def _validate_path(path: str) -> str:
        stripped = path.strip()
        if not stripped:
            raise GuidanceRejected("safe repository-relative path required")
        candidate = PurePosixPath(stripped)
        normalized = candidate.as_posix()
        if (
            not normalized
            or candidate.is_absolute()
            or ".." in candidate.parts
            or normalized.startswith(".git/")
        ):
            raise GuidanceRejected("safe repository-relative path required")
        return normalized
