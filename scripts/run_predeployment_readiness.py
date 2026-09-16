from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from enum import Enum
from hashlib import sha256
from pathlib import Path

from narang_rider.predeployment_readiness import (
    ArkaonPredeploymentInspector,
    CheckEvidence,
    IntegrationCandidate,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "arkaon-predeployment-readiness.json"
NOW = datetime(2026, 9, 16, tzinfo=UTC)
HEAD = "a9bc9fcd6fdf87cf17db0daee77459dcf8fc5beb"


def digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def normalize(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [normalize(item) for item in value]
    return value


def main() -> None:
    candidate = IntegrationCandidate(
        "rc.13-predeployment",
        HEAD,
        "8ea1e56774d9de350b9b8455e58fa98a95f94c14",
        tuple(range(1, 64)) + (72, 73, 75, 76, 78, 79, 80, 81, 82, 83, 85, 86, 87, 88),
        digest("source-tree-at-pr-88"),
    )
    internal_passes = {
        "SOURCE_LINEAGE",
        "UNIT_REGRESSION",
        "STATIC_SECURITY",
        "MIGRATION_RECOVERY",
        "SYNTHETIC_CONTRACT",
    }
    evidence = tuple(
        CheckEvidence(
            check_id,
            "INTERNAL_ENGINEERING",
            "PASS",
            digest(check_id),
            NOW - timedelta(minutes=1),
            NOW + timedelta(days=14),
            HEAD,
        )
        for check_id in sorted(internal_passes)
    )
    report = ArkaonPredeploymentInspector().inspect(
        report_id="arkaon-predeployment-rc13",
        candidate=candidate,
        evidence=evidence,
        now=NOW,
    )
    payload = normalize(asdict(report))
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_bytes(encoded)
    OUTPUT.with_suffix(OUTPUT.suffix + ".sha256").write_text(sha256(encoded).hexdigest() + "\n")
    if payload["verdict"] != "BLOCKED" or payload["deploy_allowed"]:
        raise SystemExit("predeployment report must remain fail-closed")


if __name__ == "__main__":
    main()
