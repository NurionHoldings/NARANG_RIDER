from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from enum import Enum
from hashlib import sha256
from pathlib import Path

from narang_rider.experience_operations_audit import (
    ArkaonExperienceOperationsAuditor,
    OperationalManifest,
    PageSnapshot,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "arkaon-experience-operations-audit.json"


def normalize(value):
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, Enum): return value.value
    if isinstance(value, dict): return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [normalize(item) for item in value]
    return value


def main() -> None:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    pages = (
        PageSnapshot("home", "/", "나랑라이더", (1, 2), ("/start",),
            ("나랑 달리고, 나란히 성장하다.",), ("/start",), 7.0, 44, False),
        PageSnapshot("start", "/start", "시작", (1, 2), ("submit",),
            ("합성 시작 화면",), (), 7.0, 44, False),
    )
    routes = frozenset({"/", "/start"})
    report = ArkaonExperienceOperationsAuditor().audit(
        report_id="synthetic-experience-audit", candidate_commit="e2dd9f5d55e5a3a953af565ca467bd0aa4d1c875",
        pages=pages, manifest=OperationalManifest(routes, routes, frozenset({"/start"}), frozenset({"/start"})),
        benchmarks=(), now=now,
    )
    payload = normalize(asdict(report))
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_bytes(encoded)
    OUTPUT.with_suffix(OUTPUT.suffix + ".sha256").write_text(sha256(encoded).hexdigest() + "\n")
    if payload["automatic_change_allowed"] or payload["competitor_copy_allowed"]:
        raise SystemExit("experience auditor exceeded proposal authority")


if __name__ == "__main__": main()
