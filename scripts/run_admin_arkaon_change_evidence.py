from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
from pathlib import Path

from narang_rider.admin_arkaon_control import (
    AdminArkaonControl,
    PartialChangeInstruction,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "admin-arkaon-change-evidence.json"
NOW = datetime(2026, 9, 16, tzinfo=UTC)
HEAD = "73e0713b16305b0d7120fd20d20efa19ffb9e114"


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def normalize(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    return value


def main() -> None:
    control = AdminArkaonControl()
    control.start_session("synthetic-session", operator_id="operator:synthetic", now=NOW)
    control.post_message(
        "synthetic-session", message_id="message-1", actor_type="OPERATOR",
        actor_ref="operator:synthetic", text="관리자 안내 문구만 수정해줘", now=NOW,
    )
    control.request_partial_change(
        PartialChangeInstruction(
            "instruction-1", "synthetic-session", "operator:synthetic", "관리자 UI 문구",
            ("frontend/src/admin-arkaon-console.ts",), "승인 경계를 명확하게 표시", HEAD, NOW,
        )
    )
    value = control.propose(
        proposal_id="proposal-1", instruction_id="instruction-1", source_head=HEAD,
        changed_paths=("frontend/src/admin-arkaon-console.ts",), summary="안전 문구 보완",
        patch_digest=digest("synthetic-patch"), test_plan=("frontend unit", "accessibility"),
        risk_notes=("review branch only",), now=NOW,
    )
    value = control.preview(value.proposal_id, expected_version=value.version, now=NOW)
    value = control.approve(
        value.proposal_id, operator_id="operator:synthetic", approval_digest=digest("approval"),
        expected_version=value.version, now=NOW,
    )
    value = control.apply_to_review_branch(
        value.proposal_id, applied_evidence_digest=digest("review-branch-apply"),
        expected_version=value.version, now=NOW,
    )
    payload = normalize(
        {
            "schema_version": "narang.admin-arkaon-change-evidence.v1",
            "proposal": asdict(value),
            "guidance_history": [asdict(item) for item in control.guidance(value.proposal_id)],
            "production_mutation_allowed": False,
            "automatic_merge_allowed": False,
            "automatic_deployment_allowed": False,
        }
    )
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_bytes(encoded)
    OUTPUT.with_suffix(OUTPUT.suffix + ".sha256").write_text(sha256(encoded).hexdigest() + "\n")
    if len(payload["guidance_history"]) != 4:
        raise SystemExit("every mutation must refresh guidance")


if __name__ == "__main__":
    main()
