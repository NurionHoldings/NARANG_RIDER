"""Read-only operator view for external release blockers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.validate_external_blockers import load_registry, registry_digest, validate_registry

DEFAULT_REGISTRY = Path(__file__).resolve().parents[2] / "config" / "external-blockers.json"

@dataclass(frozen=True)
class ExternalReadinessView:
    release_gate: str
    total: int
    verified: int
    pending: int
    blocked: int
    in_progress: int
    next_action_ids: tuple[str, ...]
    registry_digest: str
    generated_at: str

def build_operator_dashboard(path: Path | str = DEFAULT_REGISTRY) -> ExternalReadinessView:
    data = load_registry(path)
    validate_registry(data)
    statuses = [item["status"] for item in data["blockers"]]
    by_id = {item["id"]: item for item in data["blockers"]}
    actionable = []
    for item in data["blockers"]:
        if item["status"] == "VERIFIED":
            continue
        if all(by_id[dep]["status"] == "VERIFIED" for dep in item["dependencies"]):
            actionable.append(item["id"])
    return ExternalReadinessView(
        release_gate=data["release_gate"],
        total=len(statuses),
        verified=statuses.count("VERIFIED"),
        pending=statuses.count("PENDING"),
        blocked=statuses.count("BLOCKED"),
        in_progress=statuses.count("IN_PROGRESS"),
        next_action_ids=tuple(actionable),
        registry_digest=registry_digest(data),
        generated_at=datetime.now(UTC).isoformat(),
    )

def external_readiness_api(path: Path | str = DEFAULT_REGISTRY) -> dict[str, Any]:
    view = build_operator_dashboard(path)
    return {
        "release_gate": view.release_gate,
        "counts": {"total": view.total, "verified": view.verified, "pending": view.pending, "blocked": view.blocked, "in_progress": view.in_progress},
        "next_action_ids": list(view.next_action_ids),
        "registry_digest": view.registry_digest,
        "generated_at": view.generated_at,
        "notice": "읽기 전용 보기이며 Issue 종료·출시·병합·배포를 자동 수행하지 않습니다.",
    }
